"""tests/test_api.py — 路由层测试。

**不启动 lifespan**：`TestClient(app)` 只要不进入 `with` 块，就不会触发
lifespan，也就不会加载 BGE 模型、连 Qdrant。
依赖用 `dependency_overrides` 换成内存假对象。

这样这些测试和 harness 的测试一样快（毫秒级），
而且能在没有 Docker 的环境里跑。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import get_memory, get_retriever, get_ticket_store
from app.main import create_app
from harness.memory import SessionMemory


# --------------------------------------------------------------------------- #
# 假对象
# --------------------------------------------------------------------------- #

class _FakeVectorStore:
    def __init__(self, count: int, fail: bool = False) -> None:
        self._count = count
        self._fail = fail

    async def count(self) -> int:
        if self._fail:
            raise ConnectionError("模拟 Qdrant 不可用")
        return self._count


class _FakeRetriever:
    def __init__(self, count: int = 14, fail: bool = False) -> None:
        self.store = _FakeVectorStore(count, fail=fail)


class _FakeTicketStore:
    def __init__(self, total: int = 3) -> None:
        self._total = total

    def stats(self) -> dict[str, int]:
        return {"total": self._total, "open": self._total}


class _FakeMemory:
    def __init__(self, sessions: int = 5) -> None:
        self._sessions = sessions

    def stats(self) -> dict[str, int]:
        return {"sessions": self._sessions, "messages": self._sessions * 2}


def _client(
    *,
    retriever=None,
    tickets=None,
    memory=None,
    store_ready: bool = True,
    store_error: str = "",
) -> TestClient:
    # 显式关掉鉴权：这些测试和口令无关，也不该被 .env 里的 ACCESS_TOKEN 影响。
    app = create_app(access_token="")
    # lifespan 不会跑（TestClient 没进 with），但要模拟它设置过的状态
    app.state.store_ready = store_ready
    app.state.store_error = store_error
    app.dependency_overrides[get_retriever] = lambda: retriever or _FakeRetriever()
    app.dependency_overrides[get_ticket_store] = lambda: tickets or _FakeTicketStore()
    app.dependency_overrides[get_memory] = lambda: memory or _FakeMemory()
    return TestClient(app)


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #

def test_health_ok() -> None:
    client = _client()
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "chunks": 14,
        "tickets": 3,
        "sessions": 5,
        "detail": "",
    }


def test_health_degraded_when_store_unreachable() -> None:
    """Qdrant 连不上 → 进程仍然活着，报 degraded。

    这条守的是一个真实事故：Docker 隔夜退出后，`ensure_collection()`
    在 lifespan 里抛异常 → 整个服务退出 → 前端连页面都打不开。
    现在降级启动，至少能告警告清楚。

    注意：健康检查是**实时探**的，不是读启动时缓存的标志 ——
    所以这里要真抛异常，而不是传一个 store_ready=False。
    （曾经缓存过，后果是 Docker 晚起几秒，/health 就永远 degraded。）
    """
    client = _client(retriever=_FakeRetriever(count=0, fail=True))
    resp = client.get("/health")

    assert resp.status_code == 200          # 仍然 200，不让编排系统杀实例
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["chunks"] == 0
    assert "ConnectionError" in body["detail"]


def test_health_with_empty_index_still_200() -> None:
    """索引为空也要 200 —— 进程活着，只是没数据。

    这条守的是"健康检查不要因为依赖不可用就返回 5xx"这个约定：
    返回 503 会让编排系统杀掉实例，把一次抖动放大成全站故障。
    """
    client = _client(retriever=_FakeRetriever(count=0))
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["chunks"] == 0


def test_openapi_available() -> None:
    client = _client()
    resp = client.get("/openapi.json")

    assert resp.status_code == 200
    assert "/health" in resp.json()["paths"]


# --------------------------------------------------------------------------- #
# SessionMemory 的会话生命周期（exists / ensure）
# --------------------------------------------------------------------------- #

def test_memory_ensure_creates_session(tmp_path: Path) -> None:
    memory = SessionMemory(tmp_path / "s.db")

    assert memory.exists("s1") is False

    memory.ensure("s1")
    assert memory.exists("s1") is True

    # 重复 ensure 不报错、不产生第二条
    memory.ensure("s1")
    assert memory.stats()["sessions"] == 1


def test_memory_exists_rejects_empty() -> None:
    """空 id 一律视为不存在 —— 防止 `exists("")` 意外返回 True。"""
    memory = SessionMemory(Path(__file__).parent / "_tmp_mem.db")
    try:
        assert memory.exists("") is False
        memory.ensure("")            # 不抛异常
        assert memory.stats()["sessions"] == 0
    finally:
        memory.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(memory.db_path) + suffix).unlink(missing_ok=True)


def test_memory_ensure_then_append_keeps_single_row(tmp_path: Path) -> None:
    """ensure 建的占位行，被 append 覆盖而不是新增一条。"""
    memory = SessionMemory(tmp_path / "s.db")

    memory.ensure("s1")
    memory.append("s1", {"role": "user", "content": "你好"})

    assert memory.stats()["sessions"] == 1
    assert memory.load("s1") == [{"role": "user", "content": "你好"}]
