"""app/api/qa.py — 问答接口。

对应实现规范里的三条硬约束：

    C1  会话历史落存储层，进程内不留字典 —— 由 SessionMemory 保证
    C2  session_id 由服务端生成并下发，回传的要校验存在性
    C3  session_id 与 user_id 分离 —— 当前只有 session_id，未混用

## 会话生命周期

    首次请求：不传 session_id
              → 服务端生成 s-xxxxxxxxxxxx
              → 立刻 memory.ensure() 建一条空记录
              → 响应里返回 session_id

    后续请求：回传 session_id
              → memory.exists() 校验
              → 不存在 → 400（拒绝客户端编造的 id）

## 为什么首次请求要 ensure()

`loop.run` 只在 answered / ticket_created 时才写记忆。
如果第一次请求因为 llm_error 失败，会话不会被创建，
但服务端已经把 id 发给客户端了 —— 客户端第二次用这个 id 就会 400。

在 API 层先 ensure 一次，把这个悬空窗口关掉。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_loop, get_memory
from app.models.schemas import AskRequest, AskResponse, CitationOut
from harness.loop import AgentLoop
from harness.memory import SessionMemory

router = APIRouter(prefix="/qa", tags=["qa"])


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="向企业知识库提问",
    responses={400: {"description": "session_id 不存在"}},
)
async def ask(
    body: AskRequest,
    loop: AgentLoop = Depends(get_loop),
    memory: SessionMemory = Depends(get_memory),
) -> AskResponse:
    # ---- C2：session_id 的生成与校验 ----
    if body.session_id:
        exists = await asyncio.to_thread(memory.exists, body.session_id)
        if not exists:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"session_id 无效: {body.session_id}。"
                    "不传该字段可新建会话。"
                ),
            )
        session_id = body.session_id
    else:
        session_id = f"s-{uuid4().hex[:12]}"
        await asyncio.to_thread(memory.ensure, session_id)

    # ---- 跑 Agent ----
    result = await loop.run(body.question, session_id)

    return AskResponse(
        answer=result.answer,
        session_id=session_id,
        trace_id=result.trace_id,
        # 注意用 .value：StopReason 混入了 str，但 str() 返回的是
        # "StopReason.ANSWERED" 而不是 "answered"。这个坑有测试守着。
        stop_reason=result.stop_reason.value,
        citations=[CitationOut(**c.to_dict()) for c in result.citations],
        ticket_id=result.ticket_id,
        steps=result.steps,
        latency_ms=result.latency_ms,
        tokens=result.tokens,
    )
