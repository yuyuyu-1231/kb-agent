"""harness/memory.py — 会话记忆（SQLite 持久化）。

为什么属 harness 而不属 app：切换应用场景（知识库问答 -> 客服 -> 编码助手）
时，「加载最近 N 轮 + 滑窗截断」这套策略不变，变的只是业务规则。
归在内核侧才符合分层。

四个设计要点：

1. **整段 history 存一个 JSON 字段，不建 messages 表**
   一次会话最多 10 轮、几十条消息，全量读写的成本可以忽略。
   代价是无法按消息查询（比如"找出所有提到年假的历史"），
   但当前场景不需要。用简单的数据结构换开发速度，是这个规模下的正确选择。

2. **append 是"读-改-写"，不是原子追加**
   SQLite 单文件、单进程写入，冲突概率低。但如果以后多 worker 部署，
   这里会丢更新。真要做多进程，必须改成"每轮一行"的表结构，
   用 INSERT 而不是 UPSERT 整段 JSON。这是 v1 的已知边界。

3. **保留策略复用 context.split_into_turns**
   轮次边界必须以 user 消息为准（否则 tool_calls 配对会断）。
   这个规则已经在 context.py 里实现并测试过，这里直接复用，
   不重复实现一遍。

4. **短连接 + WAL**
   每次操作新建连接、用完即关。不做连接池，因为：
   - 连接池要处理线程安全和失效重连，复杂度不匹配收益
   - SQLite 本地连接建立是微秒级
   WAL 模式让读写可以并发（读不阻塞写），比默认的 rollback journal 更适合
   "一个请求读、另一个请求写"的场景。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .context import split_into_turns


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    """带时区的本地时间，与 trace.py 保持同一格式。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# SessionMemory
# --------------------------------------------------------------------------- #

class SessionMemory:
    """会话记忆。同步接口 —— 异步适配交给调用方（loop 用 asyncio.to_thread）。

    为什么不做成 async：
    - SQLite 的本地读写是微秒级，为它增加 await 复杂度不划算
    - 保持同步让这个模块能被纯同步单测覆盖，不需要 pytest-asyncio
    - 真要避免阻塞事件循环，调用方一句 asyncio.to_thread 就够了
    """

    def __init__(self, db_path: Path | str, keep_turns: int = 10) -> None:
        if keep_turns <= 0:
            raise ValueError("keep_turns 必须为正数")

        self.db_path = Path(db_path)
        self.keep_turns = keep_turns

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------------------------------------------------------------- 内部

    def _connect(self) -> sqlite3.Connection:
        """建一个连接。

        注意 timeout：SQLite 在写锁冲突时默认立即报 database is locked，
        设成 5 秒会自动重试，避免偶发并发写直接失败。
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:                      # with 只管事务提交/回滚，不管关闭
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id   TEXT PRIMARY KEY,
                        history_json TEXT NOT NULL,
                        updated_at   TEXT NOT NULL
                    )
                    """
                )
        finally:
            conn.close()                    # 必须显式关，with 不会帮你关

    # ---------------------------------------------------------------- 读

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """取回会话历史。不存在或损坏时返回空列表，不抛异常。"""
        if not session_id:
            return []

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT history_json FROM sessions WHERE session_id = ?",
                (session_id,),              # 参数化查询，防 SQL 注入
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return []

        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            # 数据损坏时当作空历史，而不是让整个请求 500
            return []

        return data if isinstance(data, list) else []

    # ---------------------------------------------------------------- 写

    def append(self, session_id: str, *messages: dict[str, Any]) -> int:
        """追加若干条消息，按轮次裁剪后写回。返回最终保留的消息条数。

        这是"读-改-写"，不是原子追加。单进程场景下安全，多 worker 会丢更新，
        见模块头的说明。
        """
        if not session_id or not messages:
            return 0

        history = self.load(session_id)
        history.extend(messages)
        history = self._trim(history)

        payload = json.dumps(history, ensure_ascii=False, default=str)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, history_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        history_json = excluded.history_json,
                        updated_at   = excluded.updated_at
                    """,
                    (session_id, payload, _now_iso()),
                )
        finally:
            conn.close()

        return len(history)

    def clear(self, session_id: str) -> None:
        """清空一个会话。"""
        if not session_id:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        finally:
            conn.close()

    # ---------------------------------------------------------------- 裁剪

    def _trim(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """只保留最近 keep_turns 轮。

        复用 context.split_into_turns：轮次边界必须以 user 消息为准，
        否则 assistant(tool_calls) 和 tool 消息会被拆散，下一次请求 400。
        这条规则在 context.py 里已经实现并测过，不重复造。
        """
        turns = split_into_turns(history)
        if len(turns) <= self.keep_turns:
            return history
        return [msg for turn in turns[-self.keep_turns :] for msg in turn]

    # ---------------------------------------------------------------- 观测

    def stats(self) -> dict[str, int]:
        """会话数与总消息量的粗略统计，给 /health 或调试用。"""
        conn = self._connect()
        try:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            rows = conn.execute("SELECT history_json FROM sessions").fetchall()
        finally:
            conn.close()

        total_messages = 0
        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, list):
                total_messages += len(data)

        return {"sessions": int(sessions), "messages": total_messages}
