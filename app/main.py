"""app/main.py — FastAPI 应用装配。

三件事：lifespan 建单例、注册路由、挂载静态资源。

## 为什么用 lifespan 而不是模块级全局变量

模块级 `loop = AgentLoop(...)` 会在 import 时就创建
（也就是 pytest 收集测试的瞬间就会加载 BGE 模型并连 Qdrant）。
lifespan 只在真正 `uvicorn` 启动时执行，测试里可以完全不触发。

## 为什么用 create_app() 工厂

测试要一个干净的实例，`create_app()` 每次返回新的 app，
互不干扰。模块级 `app = create_app()` 只是给 uvicorn 用的默认入口。

## 单例的生命周期

    Embedder       加载一次 BGE 模型（几百 MB），进程内复用
    QdrantStore    一个连接池
    Retriever      无状态，包了上面两个
    TicketStore    无状态（短连接）
    SessionMemory  无状态（短连接）
    AgentLoop      无状态（Guard/Tracer 都在 run() 内新建）

所以并发请求安全 —— 这是 AgentLoop 设计时就有意为之的。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health, qa
from app.core.config import get_settings
from app.infra.embedding import Embedder
from app.infra.llm import OpenAILLMClient
from app.infra.qdrant import QdrantStore
from app.services.retrieval import Retriever
from app.services.ticket import TicketStore
from app.tools.knowledge import register_knowledge_tools
from app.tools.misc import register_misc_tools
from app.tools.ticket import register_ticket_tools
from harness.loop import AgentLoop
from harness.memory import SessionMemory
from harness.tools import ToolRegistry

# app/main.py -> app -> static
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建单例，关闭时清理。"""
    settings = get_settings()

    embedder = Embedder.from_settings(settings)
    vector_store = QdrantStore.from_settings(settings)

    # 依赖不可用不应该让**进程**起不来。
    #
    # 实测踩过：Docker Desktop 隔夜退出，Qdrant 连不上，
    # `await ensure_collection()` 直接抛 —— FastAPI lifespan 失败 →
    # 整个服务退出 → 前端连页面都打不开，用户看到的是“拒绝连接”。
    #
    # 降级启动：/health 报 degraded，业务接口返回明确错误，
    # 至少页面能打开、能看到“知识库不可用”而不是一片空白。
    store_ready = True
    store_error = ""
    try:
        await vector_store.ensure_collection()
    except Exception as exc:  # noqa: BLE001
        store_ready = False
        store_error = f"{type(exc).__name__}: {exc}"
        print(f"[WARN] Qdrant 不可用，降级启动：{store_error}")

    retriever = Retriever.from_infra(embedder, vector_store)
    tickets = TicketStore(settings.sqlite_path)
    memory = SessionMemory(settings.sqlite_path)

    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)
    register_ticket_tools(registry, tickets)
    register_misc_tools(registry)

    llm = OpenAILLMClient.from_settings(settings)
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        memory=memory,
        trace_dir=settings.trace_dir,
        artifact_dir=settings.artifact_dir,
        max_steps=settings.max_steps,
        token_budget=settings.token_budget,
    )

    app.state.settings = settings
    app.state.store_ready = store_ready
    app.state.store_error = store_error
    app.state.embedder = embedder
    app.state.vector_store = vector_store
    app.state.retriever = retriever
    app.state.ticket_store = tickets
    app.state.memory = memory
    app.state.loop = loop

    yield

    await llm.aclose()
    await vector_store.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="kb-agent",
        version="0.1.0",
        description="企业知识库 Agent：检索 + 引用 + 低置信度转工单",
        lifespan=lifespan,
    )

    # 开发期放宽：前端可能从 localhost 的任意端口访问（Live Server / file://）。
    # 生产部署时前端和 API 同源，这条中间件应当删掉。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(qa.router)

    # 静态资源：前端是单文件 HTML，零构建。
    # mount 必须在路由之后：先匹配具体路由，剩下的才落到静态目录。
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            """返回单页应用。不进 OpenAPI 文档 —— 它不是 API。"""
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
