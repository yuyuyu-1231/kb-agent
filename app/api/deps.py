"""app/api/deps.py — 依赖注入。

所有"应用级单例"（AgentLoop / Retriever / TicketStore / SessionMemory）
在 main.py 的 lifespan 里创建一次，挂到 `app.state` 上。
这里只负责把它们取出来，供路由函数以 `Depends(...)` 声明式使用。

为什么不在依赖函数里创建：
- Embedder 每次新建都会重新加载 BGE 模型（几秒 + 几百 MB）
- Qdrant 客户端每次新建会泄漏连接
- 这些对象本身没有请求级可变状态：
  AgentLoop 的可变状态（Guard / Tracer / CitationRegistry）都在
  `run()` 内部新建，所以并发请求不会互相污染

用 `Request` 而不是全局变量：测试里可以替换 `app.state`，
也可以用 `app.dependency_overrides` 覆盖这些函数，两条路都通。
"""

from __future__ import annotations

from fastapi import Request

from app.services.retrieval import Retriever
from app.services.ticket import TicketStore
from harness.loop import AgentLoop
from harness.memory import SessionMemory


def get_loop(request: Request) -> AgentLoop:
    return request.app.state.loop


def get_retriever(request: Request) -> Retriever:
    return request.app.state.retriever


def get_ticket_store(request: Request) -> TicketStore:
    return request.app.state.ticket_store


def get_memory(request: Request) -> SessionMemory:
    return request.app.state.memory


def get_embedder(request: Request):
    return request.app.state.embedder


def get_vector_store(request: Request):
    return request.app.state.vector_store
