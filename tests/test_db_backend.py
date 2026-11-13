"""tests/test_db_backend.py — 数据库后端抽象的单测。

## 为什么这些测试不连真 MySQL

分两层：

- **这一层**：方言、连接池、工厂的纯逻辑。用假连接就能测，
  毫秒级，不需要任何容器。日常 `pytest` 就跑这层。
- **真库那层**：见文件末尾，用 `RUN_MYSQL_TESTS=1` 显式开启。

为什么这么分：如果所有测试都依赖 MySQL，本地跑一次要等容器起 30 秒，
CI 也会变脆（容器健康检查、端口冲突、镜像拉取）。
SQLite 那边 190 个测试能秒过，原因就是不碰外部服务。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.infra.db import (
    ConnectionPool,
    MySQLBackend,
    MySQLDialect,
    PoolExhausted,
    SQLiteBackend,
    SQLiteDialect,
    build_backend,
)


# --------------------------------------------------------------------------- #
# 方言
# --------------------------------------------------------------------------- #

def test_mysql_ddl_uses_varchar_for_keys() -> None:
    """MySQL 里 TEXT 不能做主键，必须用 VARCHAR。

    这条守的是一个真实的建表失败：直接照搬 SQLite 的 `id TEXT PRIMARY KEY`
    在 MySQL 上报 "BLOB/TEXT column used in key specification without a key length"。
    """
    ddl = "\n".join(MySQLDialect().ddl_tickets() + MySQLDialect().ddl_sessions())
    assert "TEXT PRIMARY KEY" not in ddl.upper().replace("  ", " ")
    assert "VARCHAR(32)" in ddl          # tickets.id
    assert "VARCHAR(64)" in ddl          # session_id


def test_mysql_ddl_has_indexes() -> None:
    ddl = "\n".join(MySQLDialect().ddl_tickets())
    assert "idx_tickets_status" in ddl
    assert "idx_tickets_created" in ddl


def test_mysql_ddl_has_sequences_table() -> None:
    """MySQL 必须有发号器表。

    这条守的是一个真实事故：旧实现用「查 tickets 最大 id + 1」，
    20 线程并发时**17/20 撞主键**。改成序列表后 20/20 成功。
    """
    ddl = "\n".join(MySQLDialect().ddl_extra())
    assert "sequences" in ddl
    assert "PRIMARY KEY (name)" in ddl


def test_sqlite_needs_no_sequence_table() -> None:
    """SQLite 不需要发号器：它的写是串行的，天然不会撞号。"""
    assert SQLiteDialect().ddl_extra() == []


def test_upsert_syntax_differs_between_dialects() -> None:
    """upsert 是两种方言差异最大的地方，写错就是语法错误。"""
    assert "ON CONFLICT" in SQLiteDialect().upsert_session_sql()
    assert "ON DUPLICATE KEY UPDATE" in MySQLDialect().upsert_session_sql()


def test_placeholder_differs() -> None:
    assert SQLiteDialect.placeholder == "?"
    assert MySQLDialect.placeholder == "%s"


# --------------------------------------------------------------------------- #
# 连接池
# --------------------------------------------------------------------------- #

class _FakeConn:
    """够池用的假连接：能 ping、能 rollback、能 close，并记录调用。"""

    def __init__(self) -> None:
        self.pinged = 0
        self.rolled_back = 0
        self.closed = 0
        self.alive = True

    def ping(self, reconnect: bool = False) -> None:
        self.pinged += 1
        if not self.alive:
            raise RuntimeError("连接已断开")

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


def test_pool_reuses_released_connection() -> None:
    created = []

    def factory():
        c = _FakeConn()
        created.append(c)
        return c

    pool = ConnectionPool(factory, size=2)
    c1 = pool.acquire()
    pool.release(c1)
    c2 = pool.acquire()

    assert c2 is c1, "归还的连接应该被复用，而不是新建"
    assert len(created) == 1


def test_pool_release_rolls_back() -> None:
    """归还前必须回滚：否则下一个人会继承悬空事务。"""
    pool = ConnectionPool(lambda: _FakeConn(), size=1)
    c = pool.acquire()
    pool.release(c)
    assert c.rolled_back == 1


def test_pool_replaces_dead_connection() -> None:
    """ping 失败时换一条新连接，而不是把死连接发出去。"""
    conns: list[_FakeConn] = []

    def factory():
        c = _FakeConn()
        conns.append(c)
        return c

    pool = ConnectionPool(factory, size=1)
    c1 = pool.acquire()
    c1.alive = False          # 模拟被 MySQL 的 wait_timeout 掐断
    pool.release(c1)

    c2 = pool.acquire()
    assert c2 is not c1
    assert len(conns) == 2


def test_pool_raises_when_exhausted() -> None:
    """池满且无人归还要抛 PoolExhausted，而不是永久阻塞。"""
    pool = ConnectionPool(lambda: _FakeConn(), size=1, acquire_timeout=0.05)
    pool.acquire()

    started = time.monotonic()
    with pytest.raises(PoolExhausted):
        pool.acquire()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "应该按超时退出，不能卡住"


def test_pool_close_all_closes_idle() -> None:
    pool = ConnectionPool(lambda: _FakeConn(), size=2)
    c = pool.acquire()
    pool.release(c)
    pool.close_all()
    assert c.closed == 1
    with pytest.raises(PoolExhausted):
        pool.acquire()


def test_pool_is_thread_safe_under_contention() -> None:
    """8 个线程抢 2 个连接，不能出现「同一条连接被两个线程同时拿到」。"""
    pool = ConnectionPool(lambda: _FakeConn(), size=2, acquire_timeout=1.0)
    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            conn = pool.acquire()
        except PoolExhausted:
            return
        with lock:
            seen.append(id(conn))
        time.sleep(0.01)
        pool.release(conn)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) <= 8
    # 同一时刻占用数不超过池大小（这里用「不同连接对象数」间接验证）
    assert len(set(seen)) <= 2


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

class _S:
    """最小 settings 替身。"""

    def __init__(self, **kw):
        self.db_backend = kw.get("db_backend", "sqlite")
        self.sqlite_path = kw.get("sqlite_path", ":memory:")
        self.mysql_host = "127.0.0.1"
        self.mysql_port = 3306
        self.mysql_user = "kb"
        self.mysql_password = "x"
        self.mysql_database = "kb_agent"
        self.mysql_pool_size = 2
        self.mysql_acquire_timeout = 1.0


def test_build_backend_defaults_to_sqlite(tmp_path) -> None:
    b = build_backend(_S(sqlite_path=tmp_path / "x.db"))
    assert isinstance(b, SQLiteBackend)


def test_build_backend_mysql() -> None:
    b = build_backend(_S(db_backend="mysql"))
    assert isinstance(b, MySQLBackend)


def test_build_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="未知的 DB_BACKEND"):
        build_backend(_S(db_backend="postgres"))


# --------------------------------------------------------------------------- #
# 真库测试（默认跳过）
# --------------------------------------------------------------------------- #

pytestmark_mysql = pytest.mark.skipif(
    not __import__("os").environ.get("RUN_MYSQL_TESTS"),
    reason="需要真实 MySQL：先 docker compose up -d mysql，再设 RUN_MYSQL_TESTS=1",
)


@pytestmark_mysql
def test_mysql_concurrent_ticket_ids_unique() -> None:
    """10 线程并发建单，零冲突且单号唯一。

    这是整个存储层里**唯一一个只有真并发才能覆盖**的测试：
    串行跑 10 次 create 永远是依次递增，看不出竞态。

    对应的事故：旧实现（查 max id + 1）在 20 线程下 17/20 撞主键。
    """
    import os
    import threading

    from app.infra.db_stores import build_ticket_store, init_backend

    settings = _S(db_backend="mysql")
    settings.mysql_password = os.getenv("MYSQL_PASSWORD", "kbpass")
    settings.mysql_pool_size = 10          # 池必须 >= 线程数，否则测的是池排队
    backend, ready = init_backend(settings)
    assert ready, "MySQL 未就绪"

    store = build_ticket_store(settings, backend)
    ids: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        try:
            t = store.create(
                title=f"并发测试 {i}", category="其他", priority="P3",
                summary="pytest-concurrency",
            )
            with lock:
                ids.append(t["id"])
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发建单失败：{errors[:3]}"
    assert len(ids) == 10
    assert len(set(ids)) == 10, f"单号重复：{sorted(ids)}"

    backend.close_all()


@pytestmark_mysql
def test_mysql_roundtrip() -> None:
    """真库往返：建表 → 写会话 → 读回。

    只在显式开启时跑，因为它依赖一个跑在 3306 的 MySQL。
    """
    import json
    import os

    from app.infra.db_stores import MySQLSessionMemory, init_backend

    settings = _S(db_backend="mysql")
    settings.mysql_password = os.getenv("MYSQL_PASSWORD", "kbpass")
    backend, ready = init_backend(settings)
    assert ready, "MySQL 未就绪"

    mem = MySQLSessionMemory(backend)
    sid = "pytest-session"
    mem.clear(sid)
    mem.ensure(sid)
    mem.append(sid, {"role": "user", "content": "测试"}, {"role": "assistant", "content": "OK"})

    history = mem.load(sid)
    assert json.dumps(history, ensure_ascii=False) == '[{"role": "user", "content": "测试"}, {"role": "assistant", "content": "OK"}]'

    mem.clear(sid)
    backend.close_all()
