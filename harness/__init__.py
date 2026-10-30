"""kb-agent 的 Agent 执行内核。

不依赖任何 Agent 框架，自己实现 5 个模块：
    tools.py    工具注册与 Schema 自动生成
    context.py  上下文预算管理与分级裁剪
    guard.py    失控防护（轮数 / 预算 / 重复动作）
    trace.py    轨迹落盘与回放
    loop.py     ReAct 主循环
"""

from .tools import ToolRegistry, ToolResult, ToolSpec

__all__ = ["ToolRegistry", "ToolResult", "ToolSpec"]
