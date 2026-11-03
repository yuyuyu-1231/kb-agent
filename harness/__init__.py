"""kb-agent 的 Agent 执行内核。

不依赖任何 Agent 框架，自己实现 6 个模块：

    tools.py    工具注册与 Schema 自动生成
    context.py  上下文预算管理与分级裁剪
    memory.py   会话记忆（SQLite 持久化）
    guard.py    失控防护（轮数 / 预算 / 重复动作）
    trace.py    轨迹落盘与回放
    loop.py     ReAct 主循环（尚未实现）

模块依赖关系（单向，无环）：

    tools    ──┐
    context  ──┼──> loop
    guard    ──┤
    trace    ──┤
    memory ────┘   （memory 复用 context 的 split_into_turns）

设计原则：所有"可预期的失败"用返回值表达（ToolResult / GuardDecision），
只有程序 bug 才抛异常。这样 loop 里没有一行 try/except。
"""

from .context import (
    ContextManager,
    FitResult,
    estimate_messages,
    estimate_tokens,
    split_into_turns,
)
from .guard import Guard, GuardDecision, StopReason, make_action_fingerprint
from .memory import SessionMemory
from .tools import ToolRegistry, ToolResult, ToolSpec
from .trace import Tracer, list_trace_ids, replay

__all__ = [
    # tools
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    # context
    "ContextManager",
    "FitResult",
    "estimate_tokens",
    "estimate_messages",
    "split_into_turns",
    # guard
    "Guard",
    "GuardDecision",
    "StopReason",
    "make_action_fingerprint",
    # trace
    "Tracer",
    "replay",
    "list_trace_ids",
    # memory
    "SessionMemory",
]
