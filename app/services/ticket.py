"""app/services/ticket.py — 工单存储（SQLite）。

为什么需要这一层：知识库解决 80% 的重复问题，剩下 20% 必须有人接手。
工单就是那个"交接物"——它要把用户问了什么、Agent 查过什么、
判断为什么失败，一并交给人工。这些信息如果不落库，工单就只是一句
"用户有疑问"，客服拿到还是得从头问一遍。

四个设计要点：

1. **工单号用「日期 + 当日序号」，不用 uuid**
   客服要在电话里念单号，`T-20261106-0003` 比 `a3f9c2e1` 好念得多。
   代价是要在一个事务里"查最大序号 + 插入"，并发高时会有竞争。
   本项目单进程、低频，不值得为此上分布式 ID 方案。

2. **evidence 是一个 JSON 字符串，不是自由文本**
   记录 Agent 试过哪些 query、命中多少条、最高分多少。
   人工看工单时能直接判断"这个知识库确实没有"，而不是怀疑客服偷懒。
   用 JSON 而不是散文，是为了以后能程序化统计"哪类问题最常转单"。

3. **枚举用元组白名单，不用 Enum**
   category / priority 要直接进 JSON 和 SQL，用 str 元组做校验最简单。
   Enum 还得处理序列化，而这里没有任何"类型安全"的收益。

4. **和 SessionMemory 共用同一个 .db 文件，但各自建表**
   会话历史和工单是两个业务实体，生命周期不同，但放一个文件便于备份。
   两张表互不干扰，各自的 _init_schema 只建自己的表。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# 枚举值。改这里必须同步改 app/tools/ticket.py 里的 Literal 类型，
# 否则 schema 允许的值和存储校验的值会不一致 —— 模型传了会被拒绝。
CATEGORIES: tuple[str, ...] = ("产品功能", "故障报修", "账号权限", "费用结算", "其他")
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
STATUSES: tuple[str, ...] = ("open", "in_progress", "resolved", "closed")


def _now_iso() -> str:
    """带时区的本地时间，与 trace.py / memory.py 保持同一格式。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TicketStore:
    """工单存储。同步接口，异步适配交给调用方。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------------------------------------------------------------- 内部

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        # row_factory 让查询结果可以按列名取，不必记索引位置
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
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
                    """
                )
                # 两个索引：按状态筛选（客服待办列表）、按时间排序（列表页）
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at)"
                )
        finally:
            conn.close()

    def _next_id(self, conn: sqlite3.Connection) -> str:
        """生成 T-YYYYMMDD-NNNN 形式的工单号。

        必须在事务内调用：查最大序号和插入要原子，否则并发下会撞号。
        id 都是 4 位补零，所以字符串倒序就等于序号倒序。
        """
        prefix = f"T-{datetime.now().strftime('%Y%m%d')}-"
        row = conn.execute(
            "SELECT id FROM tickets WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()

        seq = 1
        if row is not None:
            try:
                seq = int(str(row["id"]).rsplit("-", 1)[-1]) + 1
            except (ValueError, IndexError):
                seq = 1
        return f"{prefix}{seq:04d}"

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
        """创建工单，返回创建后的完整记录。

        参数在存储层再校验一次：工具层的 Literal 类型已经挡了一层，
        但存储层可能被别处直接调用（脚本、测试），不能假设上游一定校验过。
        """
        if not title or not title.strip():
            raise ValueError("title 不能为空")
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}，可选: {', '.join(CATEGORIES)}")
        if priority not in PRIORITIES:
            raise ValueError(f"未知优先级: {priority}，可选: {', '.join(PRIORITIES)}")

        now = _now_iso()
        conn = self._connect()
        try:
            # with conn 提供事务边界：_next_id 的 SELECT 和下面的 INSERT
            # 要么都成功，要么都回滚，不会出现"号被占了但单没建"。
            with conn:
                ticket_id = self._next_id(conn)
                conn.execute(
                    """
                    INSERT INTO tickets
                        (id, session_id, title, category, priority,
                         summary, evidence, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (ticket_id, session_id, title.strip(), category, priority,
                     summary, evidence, now, now),
                )
        finally:
            conn.close()

        created = self.get(ticket_id)
        return created or {}

    def update_status(self, ticket_id: str, status: str) -> bool:
        """更新工单状态。返回是否命中记录。

        v1 不做状态机校验（比如不允许从 closed 回到 open）：
        工单流转规则因公司而异，硬编码反而限制后续接入真实工单系统。
        """
        if status not in STATUSES:
            raise ValueError(f"未知状态: {status}，可选: {', '.join(STATUSES)}")
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _now_iso(), ticket_id),
                )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete(self, ticket_id: str) -> bool:
        """删除工单。演示和测试用，生产环境一般只改状态不删除。"""
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            return cursor.rowcount > 0
        finally:
            conn.close()

    # ---------------------------------------------------------------- 读

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        if not ticket_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"未知状态: {status}，可选: {', '.join(STATUSES)}")

        sql = "SELECT * FROM tickets"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(max(1, limit)))

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """总数 + 各状态计数。给 /health 和评估脚本用。"""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
            ).fetchall()
        finally:
            conn.close()

        result: dict[str, int] = {"total": int(total)}
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        return result
