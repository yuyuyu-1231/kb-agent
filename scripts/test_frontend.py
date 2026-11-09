r"""前端实测：用 Playwright + 系统 Edge 跑一遍完整问答流程。

## 为什么用 channel="msedge" 而不是下载 chromium

Playwright 默认会下载自己的 Chromium（100+ MB，装在 C 盘缓存）。
`channel="msedge"` 直接复用系统自带的 Edge，省一份下载。

## 验证点

1. **流式是真的**：采样答案区文本长度，必须是**多个不同的非零值**
   （如果是 0 -> 最终值 两个状态，说明是"攒完再返回"）
2. **四类事件都渲染了**：答案 / trace / 引用 / 结束元信息
3. **多轮会话**：第二轮带上 session_id，能解析"那"
4. **停止按钮**：点停止后页面不崩，服务端会收口

跑法（服务需已在 8000 端口）：
    python scripts/test_frontend.py
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
OUT = ROOT / "docs" / "FRONTEND_TEST.md"
URL = "http://127.0.0.1:8000/"


def answer_len_js() -> str:
    return """
    () => {
      const bubbles = document.querySelectorAll('.msg.agent .bubble');
      if (!bubbles.length) return 0;
      return bubbles[bubbles.length - 1].textContent.length;
    }
    """


async def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("# 前端实测报告")
    say("")

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(800)

        health = await page.inner_text("#health-text")
        say("## 一、页面加载")
        say("")
        say(f"- health 文本：`{health}`")
        say(f"- 页面标题：`{await page.title()}`")
        say(f"- JS 错误：{errors if errors else '无'}")
        await page.screenshot(path=str(SHOTS / "01-home.png"), full_page=True)
        say("")

        # ---------------- 第一轮 ----------------
        say("## 二、第一轮：流式 + 四类事件")
        say("")
        await page.fill("#question", "试用期员工能休年假吗？")
        await page.click("#send")

        # 每 250ms 采样一次答案长度，直到 15 秒或按钮恢复
        lengths: list[int] = []
        for _ in range(60):
            await page.wait_for_timeout(250)
            lengths.append(await page.evaluate(answer_len_js()))
            if await page.is_visible("#send") and len(lengths) > 4:
                break

        nonzero = [n for n in lengths if n > 0]
        distinct = sorted(set(nonzero))
        streaming_ok = len(distinct) >= 3

        say(f"- 采样 {len(lengths)} 次，非零 {len(nonzero)} 次")
        say(f"- 出现过的不同长度值：{distinct[:10]}{' …' if len(distinct) > 10 else ''}")
        say(f"- **流式判定：{'✅ 逐字增长' if streaming_ok else '❌ 疑似一次性返回'}**")
        say("")

        answer = await page.inner_text(".msg.agent .bubble")
        trace_rows = await page.query_selector_all(".trace-step")
        cites = await page.query_selector_all(".citation")
        meta = await page.inner_text(".msg.agent .msg-meta")

        say("| 检查项 | 结果 |")
        say("|---|---|")
        say(f"| 答案非空 | {'✅' if answer.strip() else '❌'} ({len(answer)} 字) |")
        say(f"| tool_call 渲染 | {'✅' if trace_rows else '❌'} ({len(trace_rows)} 条) |")
        say(f"| citation 渲染 | {'✅' if cites else '❌'} ({len(cites)} 条) |")
        say(f"| done 元信息 | {'✅' if meta.strip() else '❌'} `{meta}` |")
        say("")
        say("答案开头：")
        say("")
        say("```text")
        say(answer[:200])
        say("```")
        say("")
        await page.screenshot(path=str(SHOTS / "02-first-answer.png"), full_page=True)

        # ---------------- 引用双向定位 ----------------
        say("## 二·五、引用双向定位")
        say("")
        jump_ok = False
        back_ok = False
        if cites:
            try:
                idx = await page.get_attribute(".citation", "data-cite")
                await page.click(".citation")
                await page.wait_for_timeout(400)
                jump_ok = await page.query_selector(".cite-ref.flash") is not None

                await page.click(f'.cite-ref[data-cite="{idx}"]')
                await page.wait_for_timeout(400)
                back_ok = await page.query_selector(".citation.flash") is not None
            except Exception as exc:      # noqa: BLE001
                say(f"- 点击异常：{type(exc).__name__}: {exc}")
        else:
            say("- 没有引用可点（可能检索失败）")
        say(f"- 点引用卡片 → 答案里的 [n] 高亮：{'✅' if jump_ok else '❌'}")
        say(f"- 点答案里的 [n] → 引用卡片高亮：{'✅' if back_ok else '❌'}")
        say("")
        await page.screenshot(path=str(SHOTS / "05-citation-jump.png"), full_page=True)

        # ---------------- 第二轮 ----------------
        say("## 三、第二轮：会话记忆")
        say("")
        session_text = await page.inner_text("#session-info")
        say(f"- session 面板：`{session_text[:80]}`")
        say("")

        await page.fill("#question", "那病假呢？")
        await page.click("#send")

        for _ in range(60):
            await page.wait_for_timeout(250)
            if await page.is_visible("#send"):
                break

        bubbles = await page.query_selector_all(".msg.agent .bubble")
        answer2 = await bubbles[-1].inner_text() if bubbles else ""
        remember_ok = "年假" in answer2 or "转正" in answer2

        say(f"- 第二轮答案 {len(answer2)} 字")
        say(f"- **是否引用上文：{'✅' if remember_ok else '❌'}**")
        say("")
        say("```text")
        say(answer2[:200])
        say("```")
        say("")
        await page.screenshot(path=str(SHOTS / "03-second-answer.png"), full_page=True)

        # ---------------- 停止按钮 ----------------
        say("## 四、停止按钮（前端主动断连）")
        say("")
        await page.fill("#question", "把员工手册第一章到第七章全部内容逐条列出来")
        await page.click("#send")
        await page.wait_for_timeout(1200)      # 等它开始生成
        visible_stop = await page.is_visible("#stop")
        if visible_stop:
            await page.click("#stop")
            await page.wait_for_timeout(1500)

        still_alive = await page.is_visible("#composer")
        err_visible = await page.query_selector(".err")
        say(f"- 停止按钮可见：{'✅' if visible_stop else '❌'}")
        say(f"- 点击后页面仍可用：{'✅' if still_alive else '❌'}")
        say(f"- 显示中断提示：{'✅' if err_visible else '—'}")
        say("")
        await page.screenshot(path=str(SHOTS / "04-after-stop.png"), full_page=True)

        say("## 五、JS 错误汇总")
        say("")
        say(f"- {errors if errors else '无'}")
        say("")
        say("## 六、截图")
        say("")
        say("| 文件 | 内容 |")
        say("|---|---|")
        say("| `01-home.png` | 初始页面 |")
        say("| `02-first-answer.png` | 第一轮完整答案 + 引用 + trace |")
        say("| `05-citation-jump.png` | 点击引用后的定位高亮 |")
        say("| `03-second-answer.png` | 第二轮（多轮记忆） |")
        say("| `04-after-stop.png` | 点停止之后 |")
        say("")

        await browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("")
    print(f"-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
