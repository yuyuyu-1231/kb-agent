"""app/infra/db_stores.py — MySQL 版的两个存储实现。

## 为什么单独一个文件，而不是散在 memory.py / ticket.py 里

它们**共享同一套东西**：同一个连接池、同一份方言、同一个降级策略。
分开放的话，改连接池参数要改两处，而且很难一眼看出两个后端的行为差异。

## 为什么 SQLite 版一行不动

`harness/memory.py` 的 `SessionMemory` 和 `app/services/ticket.py` 的
`TicketStore` 是 **SQLite 专用**，190 个测试直接构造它们。

如果把这两个类改造成"按 backend 分支"，会出现两个问题：

1. **harness 要 import app 的代码**（拿 MySQL 后端）——违反"内核不反向依赖应用层"
2. 190 个测试的构造路径全部要重新验证

所以这里的选择是：**接口一致，实现各写一份**。

    SessionMemory     ← SQLite（内核默认，测试用）
    MySQLSessionMemory ← MySQL（部署时用）
    两者方法签名完全相同

代价是 SQL 逻辑有两份。但**方言本来就不同**（占位符、upsert 语法），
强行合并只会变成一个到处 if 的大类。

## 降级：数据库挂了不能整个服务崩

`/health` 和前端都会周期性调 `stats()`。如果 MySQL 挂了就抛异常，
健康检查会 500，前端显示"服务不可用"——但实际服务是活的，只是存储不可用。

所以这里所有方法都遵循同一条规则：

    读操作失败 → 返回空结果 + 打一行 WARN（不抛）
    写操作失败 → 抛出（写入静默丢数据比报错更糟）

`stats()` 额外返回一个 `_error` 字段，让 /health 能报 degraded。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from app.infra.db import (
    MySQLBackend,
    PoolExhausted,
    SQLiteBackend,
    build_backend,
)
from app.services.ticket import CATEGORIES, PRIORITIES, STATUSES

# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #

# 只重试"连接层"的错误。**绝不重试**唯一键冲突、SQL 语法错——
# 那类错误重试一万次也是同样的结果，只会把一次失败拖成十次。
_RETRYABLE = ("InterfaceError", "OperationalError", "PoolExhausted")


def _is_retryable(exc: Exception) -> bool:
    return any(name in type(exc).__name__ for name in _RETRYABLE)


def _with_retry(fn, *, attempts: int = 2, delay: float = 0.1):
    """连接失败重试。

    为什么只 2 次：连接池获取超时是 3s，MySQL 建连超时 5s。
    重试 2 次最坏情况约 6s——再长就该直接报错了，
    不能让用户盯着转圈。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_retryable(exc) or i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    raise last  # pragma: no cover


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 会话记忆（MySQL）
# --------------------------------------------------------------------------- #

class MySQLSessionMemory:
    """与 `harness.memory.SessionMemory` 方法签名完全一致。"""

    def __init__(self, backend: MySQLBackend, keep_turns: int = 10) -> None:
        if keep_turns <= 0:
            raise ValueError("keep_turns 必须为正数")
        self._backend = backend
        self.keep_turns = keep_turns
        # db_path 是为了让调用方（trace、诊断）能打印"数据在哪"。
        # MySQL 没有文件路径，给一个可读标识。
        from pathlib import Path

        self.db_path = Path("mysql://sessions")

    # ---------------------------------------------------------------- 读

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """取回会话历史。不存在或出错时返回空列表。

        **为什么出错也返回空列表**：记忆读失败不该让对话整个挂掉。
        模型少一点上下文还能答，但抛异常就是 500。
        """
        if not session_id:
            return []

        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT history_json FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    return cur.fetchone()
            finally:
                self._backend.close(conn)

        try:
            row = _with_retry(_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL 读会话失败 session={session_id}: {type(exc).__name__}")
            return []

        if not row:
            return []
        try:
            data = json.loads(row["history_json"])
        except (json.JSONDecodeError, TypeError, KeyError):
            return []
        return data if isinstance(data, list) else []

    def exists(self, session_id: str) -> bool:
        if not session_id:
            return False

        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 AS hit FROM sessions WHERE session_id = %s LIMIT 1",
                        (session_id,),
                    )
                    return cur.fetchone()
            finally:
                self._backend.close(conn)

        try:
            return _with_retry(_run) is not None
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL 查会话存在性失败: {type(exc).__name__}")
            return False

    def ensure(self, session_id: str) -> None:
        if not session_id:
            return

        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        # INSERT IGNORE：已存在就跳过，不覆盖历史。
                        # 用 ON DUPLICATE KEY UPDATE 会把 history 清空——不是这里要的语义。
                        cur.execute(
                            "INSERT IGNORE INTO sessions (session_id, history_json, updated_at) "
                            "VALUES (%s, %s, %s)",
                            (session_id, "[]", _now_iso()),
                        )
            finally:
                self._backend.close(conn)

        try:
            _with_retry(_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL ensure 失败 session={session_id}: {type(exc).__name__}")

    # ---------------------------------------------------------------- 写

    def append(self, session_id: str, *messages: dict[str, Any]) -> int:
        """追加消息。**写失败要抛**——静默丢历史比报错更糟。"""
        if not session_id or not messages:
            return 0

        history = self.load(session_id)
        history.extend(messages)
        history = self._trim(history)
        payload = json.dumps(history, ensure_ascii=False, default=str)
        now = _now_iso()

        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        cur.execute(
                            self._backend.dialect.upsert_session_sql(),
                            (session_id, payload, now),
                        )
            finally:
                self._backend.close(conn)

        _with_retry(_run)
        return len(history)

    def clear(self, session_id: str) -> None:
        if not session_id:
            return

        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            finally:
                self._backend.close(conn)

        _with_retry(_run)

    # ---------------------------------------------------------------- 裁剪

    def _trim(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """只保留最近 keep_turns 轮。

        复用内核对的分轮函数：轮次边界必须以 user 消息为准，
        否则 assistant(tool_calls) 和 tool 消息会被拆散，下次请求 400。
        """
        from harness.context import split_into_turns

        turns = split_into_turns(history)
        if len(turns) <= self.keep_turns:
            return history
        return [msg for turn in turns[-self.keep_turns:] for msg in turn]

    # ---------------------------------------------------------------- 观测

    def stats(self) -> dict[str, int]:
        """给 /health 用。**失败不抛**，返回 0 + _error 标记。"""

        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM sessions")
                    sessions = int((cur.fetchone() or {}).get("n", 0))
                    cur.execute("SELECT history_json FROM sessions")
                    rows = cur.fetchall()
            finally:
                self._backend.close(conn)
            return sessions, rows

        try:
            sessions, rows = _with_retry(_run)
        except Exception as exc:  # noqa: BLE001
            return {"sessions": 0, "messages": 0, "_error": type(exc).__name__}

        total = 0
        for row in rows:
            try:
                data = json.loads(row["history_json"])
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if isinstance(data, list):
                total += len(data)
        return {"sessions": sessions, "messages": total}


# --------------------------------------------------------------------------- #
# 工单（MySQL）
# --------------------------------------------------------------------------- #

class MySQLTicketStore:
    """与 `app.services.ticket.TicketStore` 方法签名完全一致。"""

    def __init__(self, backend: MySQLBackend) -> None:
        from pathlib import Path

        self._backend = backend
        self.db_path = Path("mysql://tickets")

    # ---------------------------------------------------------------- 内部

    def _next_id(self, cur) -> str:
        """T-YYYYMMDD-NNNN，从序列表取号。

        ## 为什么不用「查 tickets 最大 id + 1」

        MySQL 的**普通 SELECT 是快照读**（consistent read），不加任何锁。
        两个并发事务会读到同一个最大序号 `0003`，然后都去插 `0004`，
        第二个撞主键。

        实测（2026-11-13，`scripts/verify_mysql_concurrency.py`，20 线程）：
            成功 3 / 20，**17 个 IntegrityError**
            Duplicate entry 'T-20261113-0003' for key 'tickets.PRIMARY'

        ## 现在怎么做

        `sequences` 表里每天一行。`INSERT ... ON DUPLICATE KEY UPDATE`
        会对这一行加排他锁，并发事务在这里**排队**，序号天然不重复。

        `LAST_INSERT_ID(expr)` 是 MySQL 的技巧：把 `expr` 写进连接级的
        last_insert_id，并把它当返回值。所以不用再 `SELECT value`，
        省一次 round-trip，而且拿到的就是本事务写入的值（不会读到别人的）。
        """
        day = datetime.now().strftime("%Y%m%d")
        seq_name = f"ticket_{day}"

        # 首次取号时，先把计数器对齐到「当天已有的最大单号」。
        #
        # 不对齐会怎样：旧版本已经建了 0001-0003，而新计数器从 0 开始，
        # 第一次取号拿到 0001 → 直接撞历史主键。
        #
        # 只在行不存在时做（一次查询），不是每次建单都扫表。
        cur.execute("SELECT value FROM sequences WHERE name = %s", (seq_name,))
        if cur.fetchone() is None:
            cur.execute(
                "SELECT COALESCE(MAX(CAST(RIGHT(id, 4) AS UNSIGNED)), 0) AS mx "
                "FROM tickets WHERE id LIKE %s",
                (f"T-{day}-%",),
            )
            row = cur.fetchone() or {}
            base = int(row.get("mx") or 0)
            # INSERT IGNORE：并发下两个事务可能同时走到这里，
            # 一个插入成功、另一个被忽略，但不会报错也不会覆盖已推进的值。
            cur.execute(
                "INSERT IGNORE INTO sequences (name, value) VALUES (%s, %s)",
                (seq_name, base),
            )

        cur.execute(
            "INSERT INTO sequences (name, value) VALUES (%s, LAST_INSERT_ID(1)) "
            "ON DUPLICATE KEY UPDATE value = LAST_INSERT_ID(value + 1)",
            (seq_name,),
        )
        # LAST_INSERT_ID() 是**连接级**的，必须在同一个连接上读；
        # 事务内同一个 cursor 就是同一条连接，所以这里拿得到。
        cur.execute("SELECT LAST_INSERT_ID() AS n")
        seq = int(cur.fetchone()["n"])
        return f"T-{day}-{seq:04d}"

    # ---------------------------------------------------------------- 写

    def create(
        self,
        *,
        title: str,
        category: str,
        priority: str,
        summary: str = "",
        evidence: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        if not title or not title.strip():
            raise ValueError("title 不能为空")
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}，可选: {', '.join(CATEGORIES)}")
        if priority not in PRIORITIES:
            raise ValueError(f"未知优先级: {priority}，可选: {', '.join(PRIORITIES)}")

        now = _now_iso()

        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        ticket_id = self._next_id(cur)
                        cur.execute(
                            """
                            INSERT INTO tickets
                                (id, session_id, title, category, priority,
                                 summary, evidence, status, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)
                            """,
                            (ticket_id, session_id, title.strip(), category,
                             priority, summary, evidence, now, now),
                        )
                return ticket_id
            finally:
                self._backend.close(conn)

        ticket_id = _with_retry(_run)
        return self.get(ticket_id) or {}

    def update_status(self, ticket_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"未知状态: {status}，可选: {', '.join(STATUSES)}")

        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        affected = cur.execute(
                            "UPDATE tickets SET status = %s, updated_at = %s WHERE id = %s",
                            (status, _now_iso(), ticket_id),
                        )
                return affected > 0
            finally:
                self._backend.close(conn)

        return bool(_with_retry(_run))

    def delete(self, ticket_id: str) -> bool:
        def _run():
            conn = self._backend.connect()
            try:
                with self._backend.transaction(conn):
                    with conn.cursor() as cur:
                        affected = cur.execute(
                            "DELETE FROM tickets WHERE id = %s", (ticket_id,)
                        )
                return affected > 0
            finally:
                self._backend.close(conn)

        return bool(_with_retry(_run))

    # ---------------------------------------------------------------- 读

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        if not ticket_id:
            return None

        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
                    return cur.fetchone()
            finally:
                self._backend.close(conn)

        try:
            return _with_retry(_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL 读工单失败: {type(exc).__name__}")
            return None

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"未知状态: {status}，可选: {', '.join(STATUSES)}")

        sql = "SELECT * FROM tickets"
        params: list[Any] = []
        if status:
            sql += " WHERE status = %s"
            params.append(status)
        # LIMIT 的参数化：pymysql 会把 int 当字面量处理，不能用 %s 占位
        # （MySQL 不允许 LIMIT 用占位符绑定成字符串）。所以这里做整数转换。
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(int(max(1, limit)))

        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                self._backend.close(conn)

        try:
            return list(_with_retry(_run))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MySQL 列工单失败: {type(exc).__name__}")
            return []

    def stats(self) -> dict[str, int]:
        def _run():
            conn = self._backend.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM tickets")
                    total = int((cur.fetchone() or {}).get("n", 0))
                    cur.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")
                    rows = cur.fetchall()
            finally:
                self._backend.close(conn)
            return total, rows

        try:
            total, rows = _with_retry(_run)
        except Exception as exc:  # noqa: BLE001
            return {"total": 0, "_error": type(exc).__name__}

        result: dict[str, int] = {"total": total}
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        return result


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def build_memory(settings: Any, backend: Any = None):
    """按配置返回会话记忆实现。缺省 SQLite。

    `backend` 可以由调用方传入：main.py 在建表时已经建了一个，
    再建第二个会多出一个连接池（MySQL 场景下就是双倍的连接数）。
    """
    from harness.memory import SessionMemory

    backend = backend or build_backend(settings)
    if isinstance(backend, MySQLBackend):
        return MySQLSessionMemory(backend, keep_turns=10)
    return SessionMemory(settings.sqlite_path)


def build_ticket_store(settings: Any, backend: Any = None):
    """按配置返回工单存储实现。缺省 SQLite。"""
    from app.services.ticket import TicketStore

    backend = backend or build_backend(settings)
    if isinstance(backend, MySQLBackend):
        return MySQLTicketStore(backend)
    return TicketStore(settings.sqlite_path)


def init_backend(settings: Any) -> tuple[Any, bool]:
    """建表，返回 `(backend, ready)`。

    ## 为什么返回 backend

    MySQL 的 backend 内部持有连接池。如果这里建一个、工厂里再建一个，
    进程里就有两个池（10 条连接），而且建表用的那个永远不会被回收。

    ## 为什么失败不抛

    “数据库不可用”不是“进程该死”的理由。建表失败时返回 `ready=False`，
    服务照常启动，`/health` 报 degraded——页面能打开、能看到原因，
    而不是容器反复重启、日志里只有 traceback。

    这和 Qdrant 启动时的降级处理是同一个思路。
    """
    backend = build_backend(settings)
    from app.infra.db import init_schema, wait_for_db

    ready = True
    if isinstance(backend, MySQLBackend):
        ready = wait_for_db(backend, timeout=30.0)

    if ready:
        try:
            init_schema(backend)
        except Exception as exc:  # noqa: BLE001
            ready = False
            print(f"[WARN] 建表失败，存储降级：{type(exc).__name__}: {exc}")

    return backend, ready


__all__ = [
    "MySQLSessionMemory",
    "MySQLTicketStore",
    "build_backend",
    "build_memory",
    "build_ticket_store",
    "init_backend",
    "SQLiteBackend",
    "MySQLBackend",
    "PoolExhausted",
]
