"""app/api/health.py — 健康检查。

**永远返回 200**，把依赖状态放在 body 里。

为什么不用 503：编排系统（K8s / Docker healthcheck）用状态码决定
"要不要摘掉这个实例"。健康检查的语义是"进程还活着"，
依赖临时不可用不等于进程该死 —— 返回 503 会让编排系统杀掉所有副本，
把一次 Qdrant 抖动放大成一次全站故障。

真正要报警的是 body 里的数字（chunks 掉到 0 说明索引没了）。

三个计数都在这里暴露，是为了让"部署完到底成没成功"一眼可见：
- chunks：知识库里有几块（0 说明没入库）
- tickets：工单表有几条
- sessions：会话表有几条
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.api.deps import get_memory, get_retriever, get_ticket_store
from app.models.schemas import HealthResponse
from app.services.retrieval import Retriever
from app.services.ticket import TicketStore
from harness.memory import SessionMemory

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(
    retriever: Retriever = Depends(get_retriever),
    tickets: TicketStore = Depends(get_ticket_store),
    memory: SessionMemory = Depends(get_memory),
) -> HealthResponse:
    # SQLite 是同步库，丢线程池避免阻塞事件循环
    ticket_stats, session_stats = await asyncio.gather(
        asyncio.to_thread(tickets.stats),
        asyncio.to_thread(memory.stats),
    )
    chunks = await retriever.store.count()

    return HealthResponse(
        status="ok",
        chunks=chunks,
        tickets=int(ticket_stats.get("total", 0)),
        sessions=int(session_stats.get("sessions", 0)),
    )
