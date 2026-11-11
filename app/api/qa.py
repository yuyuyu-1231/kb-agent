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
import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_loop, get_memory
from app.models.schemas import (
    AskRequest,
    AskResponse,
    CitationOut,
    SessionHistoryResponse,
    SessionMessage,
)
from harness.loop import AgentLoop
from harness.memory import SessionMemory

router = APIRouter(prefix="/qa", tags=["qa"])


def _sse(event: str, data: dict[str, Any]) -> str:
    """拼一个 SSE 帧。

    两个细节：
    - `ensure_ascii=False`：中文不能变成 \\uXXXX，否则前端得再解一次
    - 结尾两个换行：SSE 用空行分隔事件，少一个换行浏览器不收
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _resolve_session(
    session_id: str | None,
    memory: SessionMemory,
) -> str:
    """校验或创建会话。两个端点共用。

    首次请求（不传）→ 生成并 ensure；回传的 id 必须存在，否则 400。
    """
    if session_id:
        exists = await asyncio.to_thread(memory.exists, session_id)
        if not exists:
            raise HTTPException(
                status_code=400,
                detail=f"session_id 无效: {session_id}。不传该字段可新建会话。",
            )
        return session_id

    new_id = f"s-{uuid4().hex[:12]}"
    await asyncio.to_thread(memory.ensure, new_id)
    return new_id


@router.post(
    "/ask/stream",
    summary="流式问答（SSE）",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def ask_stream(
    body: AskRequest,
    loop: AgentLoop = Depends(get_loop),
    memory: SessionMemory = Depends(get_memory),
) -> StreamingResponse:
    """流式问答。四类事件，前端按类型分别渲染。

        event: token       {"text": "年"}
        event: tool_call   {"name": "search_knowledge", "arguments": {...}}
        event: citation    {"citations": [{index, doc_name, page_no, ...}]}
        event: done        {answer, session_id, trace_id, stop_reason, ...}

    为什么用 POST 而不是 GET：问题文本进 URL 会受长度限制、会落进访问日志。
    代价是原生 EventSource 不支持 POST，前端要用 fetch + ReadableStream
    自己解析 —— 多写 20 行，换一个正确的语义。
    """
    session_id = await _resolve_session(body.session_id, memory)

    async def gen():
        try:
            async for event in loop.run_stream(body.question, session_id):
                if event.type == "done":
                    result = event.data["result"]
                    payload = AskResponse(
                        answer=result.answer,
                        session_id=session_id,
                        trace_id=result.trace_id,
                        stop_reason=result.stop_reason.value,
                        citations=[
                            CitationOut(**c.to_dict()) for c in result.citations
                        ],
                        ticket_id=result.ticket_id,
                        steps=result.steps,
                        latency_ms=result.latency_ms,
                        tokens=result.tokens,
                    ).model_dump()
                    yield _sse("done", payload)
                else:
                    yield _sse(event.type, event.data)
        except Exception as exc:  # noqa: BLE001
            # 生成器里抛异常会让连接直接断，前端看到的是“网络错误”。
            # 发一个 error 事件，至少能告诉用户发生了什么。
            yield _sse("error", {
                "detail": f"{type(exc).__name__}: {exc}",
            })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx 默认会缓冲响应，
            # 不显式关闭的话 SSE 会被攒成一整块才吐给浏览器
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/session/{session_id}",
    response_model=SessionHistoryResponse,
    summary="取回会话历史",
    responses={404: {"description": "会话不存在"}},
)
async def session_history(
    session_id: str,
    memory: SessionMemory = Depends(get_memory),
) -> SessionHistoryResponse:
    """刷新页面后恢复对话用。

    ## 为什么不把历史塞进 /ask 的 done 事件

    `done` 只应该携带“这一轮”的结果。历史是另一个维度（可能几十条，
    而且每轮都在变长），混进 SSE 会让每一帧都变大，前端还得去重。

    ## 为什么不存在返回 404 而不是空列表

    404 是给客户端的一个明确信号：“这个 id 已失效，清掉你本地存的”。
    返回空列表的话，客户端会以为“会话存在但没消息”，于是一直复用这个死 id，
    看上去就像“模型突然忘事了”。
    """
    exists = await asyncio.to_thread(memory.exists, session_id)
    if not exists:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

    rows = await asyncio.to_thread(memory.load, session_id)
    messages = [
        SessionMessage(
            role=str(row.get("role") or ""),
            content=str(row.get("content") or ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("role")
    ]
    return SessionHistoryResponse(session_id=session_id, messages=messages)


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
    session_id = await _resolve_session(body.session_id, memory)

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
