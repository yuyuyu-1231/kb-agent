"""app/tools/misc.py — 与业务无关的通用工具。

目前只有 `current_time`。

## 为什么单独一个文件

`knowledge.py` 里的两个工具都依赖 `Retriever`，`ticket.py` 的依赖
`TicketStore`。而 `current_time` 不依赖任何业务服务 —— 它只需要标准库。

按依赖划分文件，而不是按"都是工具"堆在一起：
以后要加计算器、单位换算、日期推算，都往这里放，不需要传任何服务对象。

## 为什么这个工具值得存在

模型没有"现在几点"的概念。手册里写"年休假应当在当年内使用完毕"，
用户问"今年还剩几天年假"，模型不知道"今年"是哪一年 ——
它只能回避，或者用训练数据里的日期（那是错的）。

一行代码的成本，换来一个不再被猜的时间。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harness.tools import ToolRegistry

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def register_misc_tools(registry: ToolRegistry) -> None:
    """注册通用工具。不依赖任何业务服务。"""

    @registry.tool(timeout=5)
    async def current_time() -> dict[str, Any]:
        """获取当前日期和时间。

        什么时候用：问题涉及“今年”“本月”“还剩几天”这类需要知道当下时间的内容。
        例如“今年还剩多少天年假”——你需要先知道今年是哪一年。

        什么时候不用：问题本身与时间无关时，不要调用（多一轮就是多一次延迟）。

        不需要参数。
        """
        now = datetime.now().astimezone()
        return {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": _WEEKDAYS[now.weekday()],   # weekday(): 周一=0
            "year": now.year,
            "month": now.month,
            "iso": now.isoformat(timespec="seconds"),
        }
