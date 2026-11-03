"""harness/memory.py 的单元测试。全部使用 tmp_path，不碰真实 data 目录。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from harness.memory import SessionMemory


@pytest.fixture
def mem(tmp_path: Path) -> SessionMemory:
    return SessionMemory(tmp_path / "sessions.db", keep_turns=3)


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


def make_turn(i: int) -> list[dict]:
    """一轮完整对话：user + assistant(tool_calls) + tool + assistant。"""
    return [
        {"role": "user", "content": f"问题{i}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"call_{i}", "type": "function",
                            "function": {"name": "search_knowledge",
                                         "arguments": '{"query":"x"}'}}],
        },
        {"role": "tool", "tool_call_id": f"call_{i}", "content": "结果"},
        {"role": "assistant", "content": f"答案{i}"},
    ]


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #

def test_empty_session_returns_empty_list(mem: SessionMemory) -> None:
    assert mem.load("never-seen") == []


def test_append_then_load(mem: SessionMemory) -> None:
    mem.append("s1", user("试用期能休年假吗"), assistant("可以，5 天"))
    history = mem.load("s1")
    assert len(history) == 2
    assert history[0]["content"] == "试用期能休年假吗"
    assert history[1]["content"] == "可以，5 天"


def test_append_accumulates(mem: SessionMemory) -> None:
    mem.append("s1", user("q1"), assistant("a1"))
    mem.append("s1", user("q2"), assistant("a2"))
    history = mem.load("s1")
    assert [m["content"] for m in history] == ["q1", "a1", "q2", "a2"]


def test_append_returns_message_count(mem: SessionMemory) -> None:
    assert mem.append("s1", user("q1")) == 1
    assert mem.append("s1", assistant("a1")) == 2


def test_empty_inputs_tolerated(mem: SessionMemory) -> None:
    assert mem.append("", user("x")) == 0
    assert mem.append("s1") == 0
    assert mem.load("") == []


def test_sessions_isolated(mem: SessionMemory) -> None:
    mem.append("s1", user("来自 s1"))
    mem.append("s2", user("来自 s2"))
    assert mem.load("s1")[0]["content"] == "来自 s1"
    assert mem.load("s2")[0]["content"] == "来自 s2"


# --------------------------------------------------------------------------- #
# 裁剪
# --------------------------------------------------------------------------- #

def test_trim_keeps_recent_turns(mem: SessionMemory) -> None:
    """keep_turns=3，写入 5 轮，只应保留最后 3 轮。"""
    for i in range(5):
        mem.append("s1", *make_turn(i))

    history = mem.load("s1")
    users = [m["content"] for m in history if m["role"] == "user"]
    assert users == ["问题2", "问题3", "问题4"]


def test_trim_preserves_tool_pairing(mem: SessionMemory) -> None:
    """裁剪不能拆散 tool_calls 与 tool 消息，否则下一次请求 400。"""
    for i in range(5):
        mem.append("s1", *make_turn(i))

    history = mem.load("s1")
    call_ids = {tc["id"] for m in history for tc in (m.get("tool_calls") or [])}
    tool_ids = {m["tool_call_id"] for m in history if m.get("role") == "tool"}
    assert call_ids == tool_ids
    assert len(call_ids) == 3


def test_trim_noop_when_within_limit(mem: SessionMemory) -> None:
    for i in range(3):
        mem.append("s1", *make_turn(i))
    history = mem.load("s1")
    assert len(history) == 12          # 3 轮 × 4 条


def test_keep_turns_zero_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SessionMemory(tmp_path / "x.db", keep_turns=0)


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #

def test_clear(mem: SessionMemory) -> None:
    mem.append("s1", user("q"))
    mem.clear("s1")
    assert mem.load("s1") == []


def test_clear_unknown_session_no_error(mem: SessionMemory) -> None:
    mem.clear("never-seen")
    assert mem.load("never-seen") == []


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #

def test_corrupted_json_returns_empty(mem: SessionMemory) -> None:
    """数据损坏时当作空历史，而不是让请求 500。"""
    mem.append("s1", user("q"))

    conn = sqlite3.connect(mem.db_path)
    try:
        with conn:
            conn.execute("UPDATE sessions SET history_json = ? WHERE session_id = ?",
                         ("{not valid json", "s1"))
    finally:
        conn.close()

    assert mem.load("s1") == []


def test_non_list_json_returns_empty(mem: SessionMemory) -> None:
    mem.append("s1", user("q"))
    conn = sqlite3.connect(mem.db_path)
    try:
        with conn:
            conn.execute("UPDATE sessions SET history_json = ? WHERE session_id = ?",
                         (json.dumps({"not": "a list"}), "s1"))
    finally:
        conn.close()

    assert mem.load("s1") == []


# --------------------------------------------------------------------------- #
# 持久化与观测
# --------------------------------------------------------------------------- #

def test_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    first = SessionMemory(db, keep_turns=5)
    first.append("s1", user("写进去"))

    second = SessionMemory(db, keep_turns=5)
    assert second.load("s1")[0]["content"] == "写进去"


def test_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "sessions.db"
    mem = SessionMemory(nested)
    mem.append("s1", user("q"))
    assert nested.exists()


def test_stats(mem: SessionMemory) -> None:
    mem.append("s1", *make_turn(0))
    mem.append("s2", user("单条"))
    stats = mem.stats()
    assert stats["sessions"] == 2
    assert stats["messages"] == 5          # 4 + 1
