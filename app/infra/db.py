"""app/infra/db.py — 数据库后端抽象（SQLite / MySQL）。

## 为什么要有这一层

持久化原先直接写在 `harness/memory.py` 和 `app/services/ticket.py` 里，
两边各自 `sqlite3.connect()`。要支持第二种后端，最省事的写法是：

    if backend == "mysql":
        conn = pymysql.connect(...)
    else:
        conn = sqlite3.connect(...)

**这是错的**：SQL 方言的差异不止连接方式——占位符（`?` vs `%s`）、
upsert 语法（`ON CONFLICT` vs `ON DUPLICATE KEY`）、DDL 类型（`TEXT` 主键
在 MySQL 里建不出来）全都不一样。散落的 if 会漏，而且漏了只在运行时炸。

所以把方言差异收进一个 `Dialect`：业务代码只写一份 SQL 模板，
占位符和 upsert 由 Dialect 提供。

## 为什么 MySQL 用同步驱动（pymysql）而不是 aiomysql

现有的 `SessionMemory` / `TicketStore` 都是**同步接口**，调用方
（`harness/loop.py`、API 层）统一用 `asyncio.to_thread` 包。

改成 async 的代价：
- 两个类的所有方法签名要加 `async`
- 190 个测试要加 `pytest.mark.asyncio`
- 调用方的 `to_thread` 要拆掉

收益：省一次线程池调度（微秒级）。

**不划算。** 数据库 IO 本来就是阻塞的，丢线程池是正确的处理方式，
这一层不需要感知事件循环。

## 为什么自己写连接池，不用 DBUtils

自研的理由不是"造轮子好玩"，而是**这一层要能被讲清楚**。

面试问"你的连接池怎么实现的"，答"用了 DBUtils"就没东西了；
答"固定大小 + 获取超时 + 归还时 ping 探活"能接着聊下去。

代价：自研有可能有 bug。所以池的代码要短（60 行）、要单测。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# 方言
# --------------------------------------------------------------------------- #

class Dialect(Protocol):
    """SQL 方言。只暴露业务代码真正需要的差异点。"""

    placeholder: str

    def ddl_sessions(self) -> list[str]: ...
    def ddl_tickets(self) -> list[str]: ...
    def ddl_extra(self) -> list[str]: ...
    def upsert_session_sql(self) -> str: ...


class SQLiteDialect:
    """SQLite：占位符 `?`，upsert 用 ON CONFLICT。"""

    placeholder = "?"

    def ddl_sessions(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                history_json TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        ]

    def ddl_tickets(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id          TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL,
                category    TEXT NOT NULL,
                priority    TEXT NOT NULL,
                summary     TEXT NOT NULL DEFAULT '',
                evidence    TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'open',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)",
            "CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at)",
        ]

    def ddl_extra(self) -> list[str]:
        """SQLite 不需要序列表。

        为什么：SQLite 的写是**串行**的（写事务持有库级写锁），
        所以「查最大序号 + 插入」天然不会撞；MySQL 没这个保证，
        见 MySQLTicketStore._next_id 的注释。
        """
        return []

    def upsert_session_sql(self) -> str:
        return """
            INSERT INTO sessions (session_id, history_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                history_json = excluded.history_json,
                updated_at   = excluded.updated_at
        """


class MySQLDialect:
    """MySQL：占位符 `%s`，upsert 用 ON DUPLICATE KEY UPDATE。

    两个必须处理的差异：

    1. **`TEXT` 不能做主键**：MySQL 要求主键有固定长度，
       `TEXT PRIMARY KEY` 直接报错。全部字符串键改成 `VARCHAR(n)`。
    2. **`TEXT` 不能带 DEFAULT**（8.0.13 之前）：`summary` / `evidence`
       改成 `TEXT NOT NULL`，依赖插入时总有值。
    """

    placeholder = "%s"

    def ddl_sessions(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   VARCHAR(64)  NOT NULL,
                history_json LONGTEXT     NOT NULL,
                updated_at   VARCHAR(32)  NOT NULL,
                PRIMARY KEY (session_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        ]

    def ddl_tickets(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id          VARCHAR(32)  NOT NULL,
                session_id  VARCHAR(64)  NOT NULL DEFAULT '',
                title       VARCHAR(255) NOT NULL,
                category    VARCHAR(32)  NOT NULL,
                priority    VARCHAR(8)   NOT NULL,
                summary     TEXT         NOT NULL,
                evidence    TEXT         NOT NULL,
                status      VARCHAR(16)  NOT NULL DEFAULT 'open',
                created_at  VARCHAR(32)  NOT NULL,
                updated_at  VARCHAR(32)  NOT NULL,
                PRIMARY KEY (id),
                KEY idx_tickets_status  (status),
                KEY idx_tickets_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        ]

    def ddl_extra(self) -> list[str]:
        """序列表：工单号的发号器。

        为什么需要它：旧实现用「查 tickets 的最大 id + 1」，但 MySQL 的
        普通 SELECT 是**快照读**（不加锁），多个事务会读到同一个最大序号，
        然后都去插同一个主键。

        实测（2026-11-13，20 线程并发）：**17/20 撞 IntegrityError**，
        只有 3 个成功。

        改成单独的计数行后，`UPDATE ... WHERE name = ?` 会对这一行加排他锁，
        并发事务在这里排队，序号天然不重复。
        """
        return [
            """
            CREATE TABLE IF NOT EXISTS sequences (
                name  VARCHAR(64) NOT NULL,
                value BIGINT      NOT NULL DEFAULT 0,
                PRIMARY KEY (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        ]

    def upsert_session_sql(self) -> str:
        return """
            INSERT INTO sessions (session_id, history_json, updated_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                history_json = VALUES(history_json),
                updated_at   = VALUES(updated_at)
        """


# --------------------------------------------------------------------------- #
# 连接池（只有 MySQL 用得上）
# --------------------------------------------------------------------------- #

class PoolExhausted(RuntimeError):
    """池里拿不到连接。调用方应该当成「数据库不可用」处理。"""


class ConnectionPool:
    """固定大小的连接池。

    ## 为什么是固定大小，不是自动扩缩

    自动扩缩要处理「扩到多少」「缩的时机」，还容易在流量尖峰时把数据库
    连接数打满。固定 5 个，配一个获取超时，够了。

    ## 获取为什么要有超时

    没有超时的话，数据库挂了会让所有请求**永久阻塞**在 acquire 上——
    比直接报错更糟，用户看到的是页面一直转圈。

    ## 归还时为什么要 ping

    连接可能被中间件（防火墙、MySQL 的 `wait_timeout`）单方面掐断。
    池里躺着的连接表面是好的，实际已经死了。`ping(reconnect=True)`
    会检测并按需重连，代价是一次 round-trip（本地微秒级）。
    """

    def __init__(self, factory, size: int = 5, acquire_timeout: float = 3.0) -> None:
        if size <= 0:
            raise ValueError("size 必须为正数")
        self._factory = factory
        self.size = size
        self.acquire_timeout = acquire_timeout
        self._free: queue.LifoQueue = queue.LifoQueue(maxsize=size)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self) -> Any:
        if self._closed:
            raise PoolExhausted("连接池已关闭")

        # 先用空闲的
        try:
            conn = self._free.get_nowait()
        except queue.Empty:
            # 没有空闲的：如果没到上限就新建，否则等
            with self._lock:
                can_create = self._created < self.size
                if can_create:
                    self._created += 1
            if can_create:
                try:
                    return self._factory()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise
            try:
                conn = self._free.get(timeout=self.acquire_timeout)
            except queue.Empty as exc:
                raise PoolExhausted(
                    f"连接池耗尽：{self.size} 个连接全部占用，等待 {self.acquire_timeout}s"
                ) from exc

        # 探活：死连接换一个新的
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return self._factory()
        return conn

    def release(self, conn: Any) -> None:
        if conn is None:
            return
        if self._closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            # 归还前回滚未提交的事务。不这么做的话，下一个拿到这条连接的人
            # 会继承一个悬空事务，可能看到别人的未提交数据。
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._free.put_nowait(conn)
        except queue.Full:
            # 池里已经满了（说明有人重复归还），直接关掉这条
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._free.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 后端
# --------------------------------------------------------------------------- #

class SQLiteBackend:
    """SQLite 后端：每次操作新建连接，用完即关。

    为什么不也做池：SQLite 的连接建立是**微秒级**（本地文件句柄），
    池化收益接近零，反而多一层状态。而且 SQLite 的并发模型是
    「单写多读」，池化连接并不会提高写并发（仍然受文件锁约束）。
    """

    dialect = SQLiteDialect()

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def close(self, conn: Any) -> None:
        if conn is not None:
            conn.close()

    def close_all(self) -> None:
        return

    # SQLite 的 `with conn` 管事务；MySQL 的连接对象不实现上下文管理器，
    # 所以统一用 transaction() 包一层，业务代码只写一种。
    def transaction(self, conn: Any):
        return conn


class MySQLBackend:
    """MySQL 后端：连接从池里取，用完归还。"""

    dialect = MySQLDialect()

    def __init__(
        self,
        *,
        host: str,
        port: int = 3306,
        user: str,
        password: str,
        database: str,
        pool_size: int = 5,
        connect_timeout: int = 5,
        acquire_timeout: float = 3.0,
    ) -> None:
        self._params = dict(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            # autocommit=False：事务边界由业务代码显式控制，
            # 不让驱动替我们决定什么时候提交。
            autocommit=False,
            connect_timeout=int(connect_timeout),
        )
        self._pool = ConnectionPool(
            self._new_connection, size=pool_size, acquire_timeout=acquire_timeout
        )

    def _new_connection(self):
        import pymysql

        # DictCursor：让查询结果能按列名取，和 SQLite 的 row_factory=Row 保持一致。
        # 不这么做的话，同一个业务方法要在两个后端里写两套取值方式
        # （row[0] vs row["id"]），很容易错。
        return pymysql.connect(
            cursorclass=pymysql.cursors.DictCursor, **self._params
        )

    def connect(self):
        return self._pool.acquire()

    def close(self, conn: Any) -> None:
        self._pool.release(conn)

    def close_all(self) -> None:
        self._pool.close_all()

    def transaction(self, conn: Any):
        """事务边界。

        MySQL 的连接对象不能直接 `with conn`，所以这里返回一个
        轻量的上下文管理器：正常退出提交，异常退出回滚。

        为什么不用 autocommit=True 图省事：那样每条 SQL 都是独立事务，
        「查最大工单号 → 插入」就不再原子，并发下会撞号。
        """
        return _MySQLTransaction(conn)


class _MySQLTransaction:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False       # 不吞异常


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def build_backend(settings: Any) -> SQLiteBackend | MySQLBackend:
    """按配置建后端。

    `DB_BACKEND` 缺省 = sqlite。**默认必须是 SQLite**：
    测试和本地开发不能依赖一个必须起容器的服务。
    """
    kind = (getattr(settings, "db_backend", "") or "sqlite").lower()

    if kind in ("sqlite", ""):
        return SQLiteBackend(settings.sqlite_path)

    if kind == "mysql":
        return MySQLBackend(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            pool_size=settings.mysql_pool_size,
            acquire_timeout=settings.mysql_acquire_timeout,
        )

    raise ValueError(f"未知的 DB_BACKEND: {kind}（可选 sqlite / mysql）")


def init_schema(backend: SQLiteBackend | MySQLBackend) -> None:
    """建表。两个后端共用同一段执行逻辑，DDL 由 dialect 提供。

    为什么不用迁移工具（Alembic 之类）：这个项目没有需要保留的历史数据，
    `CREATE TABLE IF NOT EXISTS` 足够。真实项目里表结构会演进，
    那时才需要 migration——但那是另一个话题，别为演示项目引入它。
    """
    conn = backend.connect()
    try:
        with backend.transaction(conn) as c:
            cur = c.cursor() if hasattr(c, "cursor") else c
            for stmt in (
                backend.dialect.ddl_sessions()
                + backend.dialect.ddl_tickets()
                + backend.dialect.ddl_extra()
            ):
                cur.execute(stmt)
            if hasattr(cur, "close"):
                cur.close()
    finally:
        backend.close(conn)


def wait_for_db(backend: SQLiteBackend | MySQLBackend, timeout: float = 30.0) -> bool:
    """启动时等数据库就绪。

    为什么需要：`docker compose up` 里 MySQL 容器和 app 是并行起的，
    依赖 `depends_on: condition: service_healthy` 只能保证「容器健康」，
    但应用侧的连接池第一次建连仍可能撞上 MySQL 还在初始化的瞬间。

    重试而不是直接失败：容器编排里的「立刻失败」会导致 app 退出，
    然后靠 restart 策略重启——日志会很难看，而且掩盖了真实原因。
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = backend.connect()
            backend.close(conn)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0)
    print(f"[WARN] 数据库在 {timeout}s 内未就绪：{type(last).__name__}: {last}")
    return False
