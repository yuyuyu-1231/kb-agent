"""app/tools/ticket.py — 工单相关的两个工具。

这两个工具是"Agent 决策环"的另一半：
    检索得到依据 → 直接回答
    检索不到    → 自主建单，把上下文交接给人工

三个设计要点：

1. **category / priority 用 Literal，不用 str**
   `Literal["产品功能", "故障报修", ...]` 会被 pydantic 生成 `enum: [...]`，
   模型在 schema 层面就只能从这几个值里选，**根本填不出错值**。
   如果写成 `category: str` 再在 docstring 里说明"可选值有……"，
   模型仍然会偶尔填"其他类别"或"技术问题"。

2. **evidence 参数是给人工看的，不是给模型看的**
   它记录"Agent 试过哪些检索词、命中多少条"。客服拿到工单时能立刻判断
   "知识库确实没有这个内容"，而不是怀疑用户没找对地方。这个字段的信息
   只能由模型提供（它知道自己查了什么），所以必须作为工具参数传进来。

3. **返回给模型的字段要精简**
   模型的返回会进上下文，每个字段都是 token。所以只返回 ticket_id、status、
   title、category、priority —— 足够它向用户复述"已建单 T-xxx，P2 优先级"。
   summary、evidence、created_at 这些是给人看的，不进模型上下文。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from app.services.ticket import TicketStore
from harness.tools import ToolRegistry

# 注意：这里的字面量必须和 app/services/ticket.py 里的 CATEGORIES / PRIORITIES
# 完全一致。一个是 schema 层的约束，一个是存储层的校验，两边对不上会出现
# "模型能填但存储层拒绝"的诡异情况。改一处必须改另一处。
Category = Literal["产品功能", "故障报修", "账号权限", "费用结算", "其他"]
Priority = Literal["P0", "P1", "P2", "P3"]


def register_ticket_tools(registry: ToolRegistry, store: TicketStore) -> None:
    """把工单能力注册到工具表。"""

    @registry.tool(timeout=10)
    async def create_ticket(
        title: str,
        category: Category,
        priority: Priority,
        summary: str,
        evidence: str = "",
    ) -> dict[str, Any]:
        """创建一个工单交给人工处理。

        什么时候用：连续检索两次仍找不到依据，或者问题涉及必须人工核实的
        具体记录（账号状态、订单金额、费用结算、系统故障），就创建工单，
        并在回答里把工单号告诉用户。

        什么时候不用：能从知识库找到答案的，直接回答即可，不要建单。

        Args:
            title: 一句话概括用户问题，不超过 30 字。
            category: 工单类别，从枚举里选最接近的一个。
            priority: 优先级。P0 系统不可用或资金损失；P1 影响多人；
                P2 单人受阻；P3 一般咨询建议。
            summary: 用户的问题描述加你已经查到什么，2-4 句。
            evidence: 你试过的检索词和命中情况，便于人工快速判断。
        """
        record = await asyncio.to_thread(
            store.create,
            title=title,
            category=category,
            priority=priority,
            summary=summary,
            evidence=evidence,
        )
        return {
            "ticket_id": record.get("id", ""),
            "status": record.get("status", "open"),
            "title": record.get("title", ""),
            "category": record.get("category", ""),
            "priority": record.get("priority", ""),
        }

    @registry.tool(timeout=10)
    async def check_ticket_status(ticket_id: str) -> dict[str, Any]:
        """查询工单的当前状态。

        用户问「我的工单处理得怎么样了」「之前那个问题有进展吗」时使用。

        Args:
            ticket_id: 工单号，形如 T-20261106-0001。从创建工单的返回里获取。
        """
        record = await asyncio.to_thread(store.get, ticket_id)
        if record is None:
            return {
                "found": False,
                "ticket_id": ticket_id,
                "message": f"未找到工单 {ticket_id}",
            }
        return {
            "found": True,
            "ticket_id": record.get("id", ""),
            "status": record.get("status", ""),
            "title": record.get("title", ""),
            "category": record.get("category", ""),
            "priority": record.get("priority", ""),
            "summary": record.get("summary", ""),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
        }
