r"""真实 HTTP 断连补验：验证中断标记真的能防住"接着半截答案乱写"。

## 和 unit test 的区别

`test_harness_loop.py` 里那条用 `gen.aclose()` 触发 GeneratorExit。
真实浏览器断连走的是 `asyncio.CancelledError`（Starlette 取消 task）。
两者共用同一个 except 分支，但**推理不等于证据**，所以要跑这一遍。

## 四个验证点

1. 点停止后，trace 里最后一次 finish 的 `stop_reason == "cancelled"`
2. 会话记忆的 assistant 内容含 `CANCELLED_MEMORY_NOTE`
3. **第二轮问一个完全不同的话题，模型不会接着上一轮半截内容写**
4. 第二轮能正常回答（说明记忆没有把上下文搞坏）

第 3 点是核心。判据：
- 第二轮答案主体必须关于住宿（含 "500" 或 "一线"）
- 开头 60 字内不能出现续写词（继续 / 接着 / 刚才 / 上次 / 未完成）

跑法（服务需已在 8000 端口，且 /health 为 ok）：
    python scripts/test_disconnect_marker.py
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings                      # noqa: E402
from harness.loop import CANCELLED_MEMORY_NOTE                # noqa: E402
from harness.memory import SessionMemory                      # noqa: E402

URL = "http://127.0.0.1:8000/"
SHOTS = ROOT / "docs" / "screenshots"
OUT = ROOT / "docs" / "DISCONNECT_REAL.md"

CONTINUATION_WORDS = ("继续", "接着", "刚才", "上次", "未完成", "刚才说")


def answer_len_js() -> str:
    return """
    () => {
      const b = document.querySelectorAll('.msg.agent .bubble');
      return b.length ? b[b.length - 1].textContent.length : 0;
    }
    """


def latest_session_ids(db_path: Path) -> list[str]:
    """按更新时间倒序列出会话。

    为什么不从页面拿 session_id：它只在 `done` 事件里返回。
    而本测试是“答到一半就停”，`done` 永远不会到 ——
    前端拿不到 id，只能从库上查。

    记忆存 SQLite，不依赖 Qdrant，所以即使知识库降级也能验。
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        rows = conn.execute(
            "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def last_finish(trace_dir: Path) -> dict | None:
    files = sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for path in reversed(files):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and "stop_reason" in rec:
                return rec
    return None


async def main() -> None:
    settings = get_settings()
    memory = SessionMemory(settings.sqlite_path)
    trace_dir = Path(settings.trace_dir)

    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("# 真实 HTTP 断连补验")
    say("")

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(600)
        say(f"- 页面 health：`{await page.inner_text('#health-text')}`")
        say("")

        # ---------------- 第一轮：问到一半点停止 ----------------
        say("## 一、第一轮：流式中途点停止")
        say("")
        await page.fill("#question", "试用期员工能休年假吗？")
        await page.click("#send")

        started = False
        for _ in range(60):
            await page.wait_for_timeout(150)
            if await page.evaluate(answer_len_js()) > 0:
                started = True
                break

        partial = await page.evaluate(answer_len_js())
        say(f"- 是否已开始输出：{'是' if started else '否'}")
        say(f"- 停止时已有 {partial} 字")

        await page.click("#stop")
        await page.wait_for_timeout(1800)

        session_text = await page.inner_text("#session-info")
        say(f"- 页面 session 面板：`{session_text.strip()[:40]}`")
        say("  （停止时 done 事件还没到，前端本来就拿不到 session_id）")

        # 从库里找最新的会话 —— 记忆不依赖 Qdrant，降级时也能验
        candidates = latest_session_ids(settings.sqlite_path)
        session_id = candidates[0] if candidates else ""
        say(f"- 从 SQLite 取到最新 session：`{session_id}`")
        say("")
        await page.screenshot(path=str(SHOTS / "06-disconnect.png"), full_page=True)

        # ---------------- 检查 trace ----------------
        finish = last_finish(trace_dir)
        stop_reason = str(finish.get("stop_reason")) if finish else "(无)"
        trace_ok = stop_reason == "cancelled"

        say("## 二、trace")
        say("")
        say(f"- 最后一次 finish 的 stop_reason：`{stop_reason}`")
        say(f"- **判定：{'✅' if trace_ok else '❌'}**")
        say("")

        # ---------------- 检查记忆标记 ----------------
        history = memory.load(session_id)
        assistant_msgs = [m for m in history if m.get("role") == "assistant"]
        assistant_content = str(assistant_msgs[-1].get("content", "")) if assistant_msgs else ""
        has_note = CANCELLED_MEMORY_NOTE.strip() in assistant_content

        say("## 三、会话记忆")
        say("")
        say(f"- 消息条数：{len(history)}")
        for m in history:
            say(f"  - `{m.get('role')}`: {str(m.get('content'))[:80]}")
        say("")
        say(f"- **是否带中断标记：{'✅' if has_note else '❌'}**")
        say("")

        # ---------------- 第二轮：问完全不同的话题 ----------------
        say("## 四、第二轮：问完全不同的话题，看会不会续写")
        say("")
        await page.fill("#question", "去北京出差住宿一晚能报多少钱？")
        await page.click("#send")

        for _ in range(80):
            await page.wait_for_timeout(250)
            if await page.is_visible("#send"):
                break

        bubbles = await page.query_selector_all(".msg.agent .bubble")
        answer2 = await bubbles[-1].inner_text() if bubbles else ""
        head = answer2[:60]

        about_hotel = ("500" in answer2) or ("一线" in answer2) or ("住宿" in answer2)
        continuation = [w for w in CONTINUATION_WORDS if w in head]

        say(f"- 第二轮答案 {len(answer2)} 字")
        say("")
        say("```text")
        say(answer2[:220])
        say("```")
        say("")
        say("| 检查项 | 结果 |")
        say("|---|---|")
        say(f"| 内容关于住宿 | {'✅' if about_hotel else '❌'} |")
        say(f"| 开头 60 字无续写词 | {'✅' if not continuation else '❌ ' + str(continuation)} |")
        say("")
        await page.screenshot(path=str(SHOTS / "07-after-disconnect-turn2.png"), full_page=True)

        # ---------------- 第二轮后的记忆 ----------------
        history2 = memory.load(session_id)
        say("## 五、第二轮之后的完整记忆")
        say("")
        for m in history2:
            say(f"  - `{m.get('role')}`: {str(m.get('content'))[:90]}")
        say("")

        say("## 六、总判定")
        say("")
        all_ok = trace_ok and has_note and about_hotel and not continuation
        say(f"- trace = cancelled：{'✅' if trace_ok else '❌'}")
        say(f"- 记忆带标记：{'✅' if has_note else '❌'}")
        say(f"- 第二轮答对题：{'✅' if about_hotel else '❌'}")
        say(f"- 第二轮没续写：{'✅' if not continuation else '❌'}")
        say("")
        say(f"**结论**：{'四项全过' if all_ok else '有项未过，见上'}")
        say("")
        say(f"> JS 错误：{errors if errors else '无'}")
        say("")

        await browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("")
    print(f"-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
