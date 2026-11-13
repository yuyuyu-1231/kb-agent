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

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health, qa
from app.api.auth import AccessControl
from app.core.config import get_settings
from app.infra.db_stores import build_memory, build_ticket_store, init_backend
from app.infra.embedding import Embedder
from app.infra.llm import OpenAILLMClient
from app.infra.qdrant import QdrantStore
from app.services.retrieval import Retriever
from app.tools.knowledge import register_knowledge_tools
from app.tools.misc import register_misc_tools
from app.tools.ticket import register_ticket_tools
from harness.loop import AgentLoop
from harness.tools import ToolRegistry

# app/main.py -> app -> static
STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _auto_ingest(settings, embedder, store) -> None:
    """collection 为空时，把 data/corpus 下的 PDF 全部入库。

    为什么需要：docker compose 新建的 Qdrant volume 是空的。
    没有这一步，容器起来了但知识库 0 个 chunk，所有问答都会降级。

    幂等性：先查 count()，>0 就直接返回。
    所以重启容器不会重复入库。

    用环境变量控制：本地开发时不想要这个行为（习惯手动跑脚本）。
    """
    if os.getenv("AUTO_INGEST", "").lower() not in ("1", "true", "yes"):
        return

    try:
        if await store.count() > 0:
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[AUTO_INGEST] 查 count 失败，跳过：{type(exc).__name__}")
        return

    corpus = Path(settings.data_dir) / "corpus"
    pdfs = sorted(corpus.glob("*.pdf"))
    if not pdfs:
        print(f"[AUTO_INGEST] {corpus} 下没有 PDF，跳过")
        return

    from app.services.ingest import parse_pdf

    for pdf in pdfs:
        try:
            chunks, _ = await asyncio.to_thread(
                parse_pdf,
                pdf,
                chunk_size=settings.chunk_size,
                overlap=settings.chunk_overlap,
            )
            if not chunks:
                print(f"[AUTO_INGEST] {pdf.name}: 没提取到文本，跳过")
                continue
            vectors = await embedder.embed_passages([c["text"] for c in chunks])
            await store.delete_document(pdf.stem)
            await store.upsert_chunks(
                doc_id=pdf.stem,
                doc_name=pdf.name,
                chunks=chunks,
                vectors=vectors,
            )
            print(f"[AUTO_INGEST] {pdf.name}: {len(chunks)} chunks")
        except Exception as exc:  # noqa: BLE001 - 单个文档失败不该阻塞启动
            print(f"[AUTO_INGEST] {pdf.name} 失败：{type(exc).__name__}: {exc}")


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

    # 容器首次启动时 Qdrant 是空的，自动把 corpus 里的 PDF 入进去
    if store_ready:
        await _auto_ingest(settings, embedder, vector_store)

    retriever = Retriever.from_infra(embedder, vector_store)

    # 存储后端：sqlite（默认）| mysql，靠 DB_BACKEND 切换。
    #
    # init_backend 会建表，并把 backend 返回来复用——不能在这里再调一次
    # build_backend，否则 MySQL 会多出一个连接池。
    # 第二个返回值是“就绪”：建表失败不阻止启动，只让 /health 报 degraded。
    db_backend, db_ready = init_backend(settings)
    tickets = build_ticket_store(settings, db_backend)
    memory = build_memory(settings, db_backend)

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
    # 存储层状态：db_ready=False 时 /health 报 degraded，但服务照常可用
    app.state.db_ready = db_ready
    app.state.db_backend = settings.db_backend
    app.state.embedder = embedder
    app.state.vector_store = vector_store
    app.state.retriever = retriever
    app.state.ticket_store = tickets
    app.state.memory = memory
    app.state.loop = loop

    yield

    await llm.aclose()
    await vector_store.close()


def create_app(
    *,
    access_token: str | None = None,
    rate_limit_per_minute: int | None = None,
) -> FastAPI:
    """建一个应用实例。

    两个参数显式传入时覆盖 .env 同名配置：测试里
    `create_app(access_token="secret")` 就能验鉴权，不用 monkeypatch 环境变量；
    `create_app(access_token="")` 则把鉴权关掉，让与鉴权无关的测试不被干扰。
    """
    settings = get_settings()
    token = settings.access_token if access_token is None else access_token
    limit = (
        settings.rate_limit_per_minute
        if rate_limit_per_minute is None
        else rate_limit_per_minute
    )

    app = FastAPI(
        title="kb-agent",
        version="0.1.0",
        description="企业知识库 Agent：检索 + 引用 + 低置信度转工单",
        lifespan=lifespan,
    )

    # 顺序有讲究：Starlette 是「后添加的在外层」。
    #
    # 访问控制加在 CORS **之前** → 运行时 CORS 在外层包住它，
    # 401/429 响应也会带上 CORS 头。反过来（鉴权在外层）的话，
    # 浏览器只会报 “CORS error”，看不到真实的 401，排查会绕远路。
    # 另外 CORS 的预检 OPTIONS 由外层直接应答，不会打到鉴权上。
    _warn_if_multi_worker(token)
    app.add_middleware(
        AccessControl,
        token=token,
        rate_limit_per_minute=limit,
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


def _warn_if_multi_worker(token: str) -> None:
    """多 worker 时提醒：限流额度会变成 N 倍。

    ## 为什么要这个

    `RateLimiter` 的计数存在**进程内存**里。`uvicorn --workers 4` 就是 4 个
    独立进程，各自算 20 次/分钟 —— 实际额度变成 80。

    实测（2026-11-13，`scripts/verify_rate_limit.py`）：
        1 worker  100 次请求 → 200 × 20，429 × 80
        2 workers 100 次请求 → 200 × 40，429 × 60
    精确 2 倍。

    ## 为什么不直接改成 Redis

    本项目部署是单容器单 worker，进程内计数就是**精确的**。
    为一个不存在的场景引入 Redis（多一个容器 + 一个依赖）不划算。
    但真有人改成多 worker 时，至少日志里会有一行提醒，不至于蒙在鼓里。

    `WEB_CONCURRENCY` 是 uvicorn/gunicorn 在多 worker 时设置的环境变量。
    """
    if not token:
        return
    try:
        workers = int(os.getenv("WEB_CONCURRENCY", "1") or 1)
    except ValueError:
        workers = 1
    if workers > 1:
        print(
            f"[WARN] 检测到 {workers} 个 worker：限流状态在进程内，"
            f"实际额度会变成配置值的 {workers} 倍。"
            f"要精确控频需换成 Redis（INCR + EXPIRE）。"
        )


app = create_app()
