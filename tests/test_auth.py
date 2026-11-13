"""tests/test_auth.py — 公网访问控制（口令 + 限流）。

## 三条要守住的约定

1. **`ACCESS_TOKEN` 为空 → 完全不拦。**
   本地开发和其余 175 个测试都依赖这个默认值。

2. **非空 → 只放行 `/`、`/health`、`/static/*`，其余必须带 `X-Access-Token`。**
   首页放行是因为"页面都打不开就没地方输口令"；
   `/docs` 和 `/openapi.json` 故意不放行 —— 公网暴露接口清单等于给扫描器送地图。

3. **先限流、后鉴权。**
   反过来的话，暴力猜口令的请求不消耗配额，等于免费重试。

## 为什么不用 monkeypatch 环境变量

`create_app(access_token=...)` 直接覆盖配置，测试之间互不影响，
也不会因为有人往 `.env` 里填了口令就让整套测试变红。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.deps import get_memory, get_retriever, get_ticket_store
from app.main import create_app

TOKEN = "correct-horse-battery-staple"


# --------------------------------------------------------------------------- #
# 假依赖（/health 要它们，鉴权本身不需要）
# --------------------------------------------------------------------------- #

class _FakeStore:
    async def count(self) -> int:
        return 14


class _FakeRetriever:
    def __init__(self) -> None:
        self.store = _FakeStore()


class _FakeTicketStore:
    def stats(self) -> dict[str, int]:
        return {"total": 0, "open": 0}


class _FakeMemory:
    def stats(self) -> dict[str, int]:
        return {"sessions": 0, "messages": 0}


def _client(token: str, *, limit: int = 60) -> TestClient:
    app = create_app(access_token=token, rate_limit_per_minute=limit)
    app.state.store_ready = True
    app.state.store_error = ""
    app.dependency_overrides[get_retriever] = lambda: _FakeRetriever()
    app.dependency_overrides[get_ticket_store] = lambda: _FakeTicketStore()
    app.dependency_overrides[get_memory] = lambda: _FakeMemory()
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 开关本身
# --------------------------------------------------------------------------- #

def test_disabled_when_token_is_empty() -> None:
    """口令为空 → 一律放行。本地开发和其余测试依赖这个默认值。"""
    client = _client("")

    assert client.get("/openapi.json").status_code == 200


def test_requires_token_when_enabled() -> None:
    client = _client(TOKEN)
    resp = client.get("/openapi.json")

    assert resp.status_code == 401
    assert "口令" in resp.json()["detail"]


def test_wrong_token_is_rejected() -> None:
    client = _client(TOKEN)
    resp = client.get("/openapi.json", headers={"X-Access-Token": "guess"})

    assert resp.status_code == 401


def test_correct_token_is_accepted() -> None:
    client = _client(TOKEN)
    resp = client.get("/openapi.json", headers={"X-Access-Token": TOKEN})

    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #

def test_health_is_public() -> None:
    """探活和前端状态灯不能要口令，否则页面一进来就是一片红。"""
    client = _client(TOKEN)

    assert client.get("/health").status_code == 200


def test_index_is_public() -> None:
    """首页必须能打开 —— 不然用户没地方输入口令。"""
    client = _client(TOKEN)

    assert client.get("/").status_code == 200


def test_docs_are_protected() -> None:
    """`/docs` 不在白名单：公网暴露接口清单等于给扫描器送地图。"""
    client = _client(TOKEN)

    assert client.get("/docs").status_code == 401


# --------------------------------------------------------------------------- #
# 限流
# --------------------------------------------------------------------------- #

def test_rate_limit_returns_429() -> None:
    client = _client(TOKEN, limit=3)
    headers = {"X-Access-Token": TOKEN}

    codes = [client.get("/openapi.json", headers=headers).status_code for _ in range(4)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert "频繁" in client.get("/openapi.json", headers=headers).json()["detail"]


def test_rate_limit_counts_public_paths_too() -> None:
    """白名单只免口令，不免限流。

    否则别人可以拿 `/health` 当免费的压测入口。
    """
    client = _client(TOKEN, limit=2)

    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 429


def test_rate_limit_applies_before_auth() -> None:
    """猜口令也要消耗配额 —— 这是"先限流后鉴权"的直接证据。

    limit=2：两次错口令之后，第三次连"鉴权"这一步都不该走到，
    直接 429。如果顺序反了，这里会一直是 401。
    """
    client = _client(TOKEN, limit=2)

    assert client.get("/openapi.json", headers={"X-Access-Token": "a"}).status_code == 401
    assert client.get("/openapi.json", headers={"X-Access-Token": "b"}).status_code == 401
    assert client.get("/openapi.json", headers={"X-Access-Token": "c"}).status_code == 429


def test_rate_limit_429_body_and_headers() -> None:
    """429 的响应体要说清楚“多久几次”，且目前**没有 Retry-After**。

    锁住“没有 Retry-After”这条不是为了守住现状，是因为
    `docs/KERNEL.md` 的面试话术里明写了这一点。
    以后真加上了，这条测试会红，逼着同步改话术 —— 否则说的和做的不一致。
    """
    client = _client(TOKEN, limit=1)
    headers = {"X-Access-Token": TOKEN}
    client.get("/openapi.json", headers=headers)         # 用掉唯一的名额
    resp = client.get("/openapi.json", headers=headers)

    assert resp.status_code == 429
    assert resp.json()["detail"] == "请求过于频繁：每 IP 每分钟最多 1 次"
    assert "retry-after" not in {k.lower() for k in resp.headers}


def test_rate_limit_per_ip_independent() -> None:
    """不同 IP 各自计数。

    为什么不用 TestClient：它的 `scope["client"]` 永远是 `testclient`，
    伪造不出两个 IP。直接调 ASGI 中间件、自己构造 scope 才能验证这一点。

    实测（2026-11-13）：A 的第 21 次 429，同时 B 的第 1 次 200。
    """
    import asyncio

    from app.api.auth import AccessControl

    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = AccessControl(ok_app, token="t", rate_limit_per_minute=2)

    async def call(ip: str) -> int:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/x",
            "headers": [(b"x-access-token", b"t")],
            "client": (ip, 1234),
        }
        out = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            out.append(msg)

        await mw(scope, receive, send)
        return next(m["status"] for m in out if m["type"] == "http.response.start")

    async def run():
        a = [await call("1.1.1.1") for _ in range(3)]
        b_first = await call("2.2.2.2")
        b_third = [await call("2.2.2.2") for _ in range(2)]
        return a, b_first, b_third

    a, b_first, b_rest = asyncio.run(run())

    assert a == [200, 200, 429], f"A 应该是 200/200/429，实际 {a}"
    assert b_first == 200, "A 打满不应该影响 B"
    assert b_rest == [200, 429], f"B 应该独立计数到 429，实际 {b_rest}"


def test_warns_when_multiple_workers(monkeypatch, capsys) -> None:
    """多 worker 启动时必须打一行警告。

    实测：2 workers 时额度从 20 变成 40（`scripts/verify_rate_limit.py`）。
    这是个已知边界，不能靠人记 —— 所以启动时主动提醒。
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    create_app(access_token=TOKEN)

    out = capsys.readouterr().out
    assert "2 个 worker" in out
    assert "2 倍" in out


def test_no_warning_for_single_worker(monkeypatch, capsys) -> None:
    """单 worker 不应该有这条警告（否则日志里噪音太多，真警告会被忽略）。"""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    create_app(access_token=TOKEN)

    out = capsys.readouterr().out
    assert "个 worker" not in out
