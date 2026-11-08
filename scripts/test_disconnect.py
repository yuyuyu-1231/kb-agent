r"""中途断开测试：验证客户端断连后 trace 和会话记忆仍写入。

## 为什么要测这个

流式模式下有个容易漏掉的路径：

    用户关页面
      → 浏览器断开连接
      → StreamingResponse 停止迭代
      → 向 run_stream 的 yield 处注入 GeneratorExit
      → run_stream 从 yield 处抛出，跳过循环之后的所有代码

如果 `tracer.finish()` 和写记忆还放在循环后面，**用户一关页面，
这次 run 的 trace 就是残缺的、这轮对话也不会进历史**。

修法是把收口放进 `finally`。本脚本验证它真的生效。

## 怎么模拟「断开」

用 httpx 的流式接口，**读到第一个 token 就主动 break**，
然后退出 `async with` —— 这会关闭 TCP 连接。

服务端下一次写 token 时收到 RST，`yield` 处抛 GeneratorExit。

## 判定

    trace     最新文件行数增加，且最后一条 finish 的 stop_reason = "cancelled"
    session   memory.load(session_id) 至少有 user + assistant 两条

跑法（需要 uvicorn 已在 8000 端口运行）：
    python scripts/test_disconnect.py
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings   # noqa: E402
from harness.memory import SessionMemory   # noqa: E402

URL = "http://127.0.0.1:8000/qa/ask/stream"
OUT = ROOT / "docs" / "DISCONNECT.md"


def latest_trace_file(trace_dir: Path) -> Path | None:
    files = sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def line_count(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def last_finish_record(trace_dir: Path) -> dict | None:
    """找最后一次 run 的收口记录（带 stop_reason 的那条）。"""
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

    session_id = f"disc-{uuid4().hex[:8]}"
    memory.ensure(session_id)

    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("# 中途断开测试")
    say("")
    say(f"- session_id: `{session_id}`")
    say("- 方法：读到第一个 token 就主动断开连接")
    say("")

    trace_file = latest_trace_file(trace_dir)
    lines_before = line_count(trace_file)

    # ---- 发请求，读到第一个 token 就断 ----
    got_token = False
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            URL,
            json={"question": "试用期员工能休年假吗？", "session_id": session_id},
        ) as resp:
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "event: token" in buf:
                    got_token = True
                    break
        # 退出 async with：连接关闭

    say("## 一、断开动作")
    say("")
    say(f"- 读到第一个 token：{'是' if got_token else '否'}")
    say("- 已主动关闭连接")
    say("")

    # 给服务端 finally 留执行时间
    await asyncio.sleep(4)

    # ---- 检查 trace ----
    lines_after = line_count(latest_trace_file(trace_dir))
    finish = last_finish_record(trace_dir)

    say("## 二、trace")
    say("")
    say(f"- 断开前行数：{lines_before}")
    say(f"- 断开后行数：{lines_after}")
    if finish:
        say(f"- 最后一条 finish 的 stop_reason：`{finish.get('stop_reason')}`")
        say(f"- extra：`{finish.get('extra')}`")
    else:
        say("- **没找到 finish 记录**")
    say("")

    # ---- 检查会话记忆 ----
    history = memory.load(session_id)
    say("## 三、会话记忆")
    say("")
    say(f"- 消息条数：{len(history)}")
    for msg in history:
        role = msg.get("role")
        content = str(msg.get("content") or "")[:70]
        say(f"  - `{role}`: {content}")
    say("")

    # ---- 判定 ----
    ok_trace = (
        lines_after > lines_before
        and finish is not None
        and str(finish.get("stop_reason")) == "cancelled"
    )
    ok_mem = len(history) >= 2

    say("## 四、判定")
    say("")
    say(f"- {'✅' if ok_trace else '❌'} trace 写入，且 stop_reason = `cancelled`")
    say(f"- {'✅' if ok_mem else '❌'} 会话记忆写入（应含 user + assistant 两条）")
    say("")
    say("**结论**：" + (
        "修复生效 —— 断开后 trace 和记忆都写了。"
        if ok_trace and ok_mem
        else "仍有问题，需要检查 finally 是否被执行。"
    ))
    say("")
    say("## 五、这说明什么")
    say("")
    say("`finally` 在 `GeneratorExit` 时也会执行，这是 Python 语言保证的。")
    say("所以把「收口」而不是「发 done 事件」放进 finally 是安全的：")
    say("")
    say("- **收口**（tracer.finish / 写记忆）应该在 finally —— 三种结局都要做")
    say("- **发 done 事件**应该在正常路径 —— 客户端都断了，再 yield 没人收")
    say("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("")
    print(f"-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
