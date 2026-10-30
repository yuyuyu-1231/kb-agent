"""harness/context.py 的单元测试。全 mock，无外部依赖。"""

from __future__ import annotations

import json
from typing import Any

from harness.context import (
    ContextManager,
    estimate_messages,
    estimate_tokens,
    split_into_turns,
)


# --------------------------------------------------------------------------- #
# 构造 messages 的小工具
# --------------------------------------------------------------------------- #

def make_tool_call(call_id: str, name: str = "search_knowledge") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"query":"年假"}'},
    }


def make_retrieval_content(n_chunks: int = 5, chunk_len: int = 200) -> str:
    return json.dumps(
        [{"text": "x" * chunk_len, "score": round(0.9 - i * 0.1, 2)} for i in range(n_chunks)],
        ensure_ascii=False,
    )


def make_turn(i: int, n_chunks: int = 5, chunk_len: int = 200) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"第{i}个问题：试用期能休年假吗"},
        {"role": "assistant", "content": None, "tool_calls": [make_tool_call(f"call_{i}")]},
        {
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": make_retrieval_content(n_chunks, chunk_len),
        },
        {"role": "assistant", "content": f"第{i}个答案"},
    ]


def assert_pairing_intact(messages: list[dict[str, Any]]) -> None:
    """断言 tool_calls 与 tool 消息一一配对。

    这是裁剪最容易踩的坑：删掉一半会导致下一次 API 请求直接 400。
    """
    call_ids = {tc["id"] for m in messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert call_ids == tool_ids, f"配对断裂: tool_calls={call_ids} tool={tool_ids}"


# --------------------------------------------------------------------------- #
# token 估算
# --------------------------------------------------------------------------- #

def test_estimate_tokens_basic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefghij") == 3          # 10 * 0.3 -> ceil 3
    assert estimate_tokens("你好abc") == 3             # 2*1.0 + 3*0.3 = 2.9 -> 3


def test_estimate_messages_counts_tool_calls() -> None:
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None, "tool_calls": [make_tool_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    total = estimate_messages(msgs)
    plain = estimate_messages([{"role": "user", "content": "问题"}])
    assert total > plain, "tool_calls 的 name/arguments 也必须计入"


# --------------------------------------------------------------------------- #
# 轮次切分
# --------------------------------------------------------------------------- #

def test_split_into_turns() -> None:
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    turns = split_into_turns(msgs)
    assert len(turns) == 2
    assert turns[0][0]["content"] == "q1"
    assert turns[1][0]["content"] == "q2"


def test_split_keeps_tool_sequence_in_one_turn() -> None:
    """assistant(tool_calls) + tool 必须落在同一轮。"""
    turns = split_into_turns(make_turn(0))
    assert len(turns) == 1
    assert len(turns[0]) == 4


# --------------------------------------------------------------------------- #
# 未超预算
# --------------------------------------------------------------------------- #

def test_no_trim_when_within_budget() -> None:
    cm = ContextManager(budget=100_000)
    msgs = make_turn(0)
    result = cm.fit(msgs)
    assert result.trimmed is False
    assert result.actions == []
    assert result.messages is msgs, "未裁剪时应原样返回同一个对象"


# --------------------------------------------------------------------------- #
# 第 1 级：截断超长工具结果
# --------------------------------------------------------------------------- #

def test_truncate_long_tool_output() -> None:
    cm = ContextManager(budget=500, tool_result_max_chars=100)
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None,
         "tool_calls": [make_tool_call("c1", "read_document_page")]},
        {"role": "tool", "tool_call_id": "c1", "content": "A" * 5000},
    ]
    result = cm.fit(msgs)

    assert result.trimmed is True
    tool_msg = next(m for m in result.messages if m["role"] == "tool")
    assert "truncated" in tool_msg["content"]
    assert "原始 5000" in tool_msg["content"]
    assert len(tool_msg["content"]) < 5000


def test_truncate_does_not_touch_short_output() -> None:
    cm = ContextManager(budget=10_000, tool_result_max_chars=2000)
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "short"}]
    result = cm.fit(msgs)
    assert result.messages[0]["content"] == "short"


# --------------------------------------------------------------------------- #
# 第 2 级：检索结果按相关度降级
# --------------------------------------------------------------------------- #

def test_shrink_retrieval() -> None:
    cm = ContextManager(budget=300, tool_result_max_chars=5000, retrieval_keep_steps=(3, 1))
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None, "tool_calls": [make_tool_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": make_retrieval_content(5, 200)},
    ]
    result = cm.fit(msgs)

    tool_msg = next(m for m in result.messages if m["role"] == "tool")
    assert "已按相关度丢弃" in tool_msg["content"]

    payload = json.loads(tool_msg["content"].split("\n[")[0])
    assert len(payload) < 5, "低分片段应被丢弃"
    # 保留的必须是最前面的（高分）片段
    assert payload[0]["score"] == 0.9


def test_shrink_skips_non_retrieval_tools() -> None:
    """非 search_knowledge 的工具结果不能被当作 chunk 列表裁剪。"""
    cm = ContextManager(budget=200, tool_result_max_chars=5000, retrieval_keep_steps=(1,))
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None,
         "tool_calls": [make_tool_call("c1", "read_document_page")]},
        {"role": "tool", "tool_call_id": "c1", "content": make_retrieval_content(5, 200)},
    ]
    cm.fit(msgs)
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    # 可能被兜底替换，但不应出现「按相关度丢弃」这种针对检索的文案
    assert "已按相关度丢弃" not in tool_msg["content"]


# --------------------------------------------------------------------------- #
# 第 3 级：历史滑窗（配对完整性是核心）
# --------------------------------------------------------------------------- #

def test_keep_recent_turns_preserves_pairing() -> None:
    cm = ContextManager(budget=10, keep_recent_turns=2)
    msgs: list[dict[str, Any]] = []
    for i in range(5):
        msgs.extend(make_turn(i))

    result = cm.fit(msgs)

    assert_pairing_intact(result.messages)
    users = [m for m in result.messages if m.get("role") == "user"]
    assert len(users) == 2, "只保留最近 2 轮"
    assert users[-1]["content"].startswith("第4个问题")


def test_keep_recent_turns_counts_from_end() -> None:
    cm = ContextManager(budget=10, keep_recent_turns=3)
    msgs: list[dict[str, Any]] = []
    for i in range(6):
        msgs.extend(make_turn(i))
    result = cm.fit(msgs)
    users = [m["content"] for m in result.messages if m.get("role") == "user"]
    assert users == ["第3个问题：试用期能休年假吗", "第4个问题：试用期能休年假吗", "第5个问题：试用期能休年假吗"]


# --------------------------------------------------------------------------- #
# 第 4 级：兜底只替换内容，不删消息
# --------------------------------------------------------------------------- #

def test_bottom_out_keeps_messages() -> None:
    """极端预算下，兜底也必须保住消息结构（只换内容）。"""
    cm = ContextManager(budget=1, keep_recent_turns=10, tool_result_max_chars=5000)
    msgs: list[dict[str, Any]] = []
    for i in range(3):
        msgs.extend(make_turn(i, n_chunks=3, chunk_len=1000))

    result = cm.fit(msgs)

    assert_pairing_intact(result.messages)
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3, "消息条数不能变，否则配对会断"
    assert any("已省略" in m["content"] for m in tool_msgs)


# --------------------------------------------------------------------------- #
# 综合
# --------------------------------------------------------------------------- #

def test_original_messages_not_mutated() -> None:
    """fit 不能修改调用方传入的对象（浅拷贝保护）。"""
    cm = ContextManager(budget=10, keep_recent_turns=1)
    msgs: list[dict[str, Any]] = []
    for i in range(3):
        msgs.extend(make_turn(i))
    snapshot_users = sum(1 for m in msgs if m.get("role") == "user")

    cm.fit(msgs)

    assert sum(1 for m in msgs if m.get("role") == "user") == snapshot_users


def test_actions_recorded() -> None:
    cm = ContextManager(budget=10, keep_recent_turns=1, tool_result_max_chars=50)
    msgs: list[dict[str, Any]] = []
    for i in range(4):
        msgs.extend(make_turn(i, chunk_len=300))

    result = cm.fit(msgs)
    assert result.trimmed is True
    assert result.actions, "裁剪动作必须被记录，供 trace 与评估统计"
    assert result.estimated_tokens <= result.original_tokens
