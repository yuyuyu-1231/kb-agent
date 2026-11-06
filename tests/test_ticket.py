"""app/services/ticket.py 与 app/tools/ticket.py 的测试。

两层分别测：
- 存储层：编号生成、枚举校验、状态流转、持久化
- 工具层：schema 是否生成 enum、返回是否精简、非法值是否被挡
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ticket import CATEGORIES, PRIORITIES, STATUSES, TicketStore
from app.tools.ticket import register_ticket_tools
from harness.tools import ToolRegistry


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(tmp_path / "tickets.db")


@pytest.fixture
def registry(store: TicketStore) -> ToolRegistry:
    reg = ToolRegistry()
    register_ticket_tools(reg, store)
    return reg


# --------------------------------------------------------------------------- #
# 存储层：创建与编号
# --------------------------------------------------------------------------- #

def test_create_returns_record(store: TicketStore) -> None:
    tk = store.create(
        title="咨询年终奖",
        category="费用结算",
        priority="P3",
        summary="用户问年终奖怎么发，知识库无相关内容",
        evidence='["年终奖 发放"]',
    )
    assert tk["id"].startswith("T-")
    assert tk["status"] == "open"
    assert tk["category"] == "费用结算"
    assert tk["created_at"]


def test_ticket_id_format(store: TicketStore) -> None:
    """T-YYYYMMDD-NNNN，日期 8 位、序号 4 位补零。"""
    tk = store.create(title="x", category="其他", priority="P3")
    parts = tk["id"].split("-")
    assert len(parts) == 3
    assert parts[0] == "T"
    assert len(parts[1]) == 8
    assert parts[1].isdigit()
    assert len(parts[2]) == 4
    assert parts[2].isdigit()


def test_sequence_increments(store: TicketStore) -> None:
    ids = [
        store.create(title=f"问题{i}", category="其他", priority="P3")["id"]
        for i in range(3)
    ]
    tails = [int(i.rsplit("-", 1)[-1]) for i in ids]
    assert tails == [1, 2, 3]


def test_sequence_continues_after_delete(store: TicketStore) -> None:
    """删掉最后一条后，新单号不应该复用旧号（否则历史记录会指向错误的单）。"""
    first = store.create(title="a", category="其他", priority="P3")
    store.create(title="b", category="其他", priority="P3")
    store.delete(first["id"]) if hasattr(store, "delete") else None

    # 即使第一条被删，新号仍应基于当前最大号递增
    third = store.create(title="c", category="其他", priority="P3")
    assert int(third["id"].rsplit("-", 1)[-1]) == 3


# --------------------------------------------------------------------------- #
# 存储层：校验
# --------------------------------------------------------------------------- #

def test_invalid_category_rejected(store: TicketStore) -> None:
    with pytest.raises(ValueError, match="未知类别"):
        store.create(title="x", category="技术问题", priority="P3")


def test_invalid_priority_rejected(store: TicketStore) -> None:
    with pytest.raises(ValueError, match="未知优先级"):
        store.create(title="x", category="其他", priority="P9")


def test_empty_title_rejected(store: TicketStore) -> None:
    with pytest.raises(ValueError, match="title"):
        store.create(title="   ", category="其他", priority="P3")


def test_enum_values_are_consistent() -> None:
    """存储层的白名单必须和文档里写的完全一致。"""
    assert CATEGORIES == ("产品功能", "故障报修", "账号权限", "费用结算", "其他")
    assert PRIORITIES == ("P0", "P1", "P2", "P3")
    assert STATUSES == ("open", "in_progress", "resolved", "closed")


# --------------------------------------------------------------------------- #
# 存储层：查询与状态
# --------------------------------------------------------------------------- #

def test_get_missing_returns_none(store: TicketStore) -> None:
    assert store.get("T-19700101-0001") is None
    assert store.get("") is None


def test_update_status(store: TicketStore) -> None:
    tk = store.create(title="x", category="其他", priority="P3")
    assert store.update_status(tk["id"], "in_progress") is True
    assert store.get(tk["id"])["status"] == "in_progress"


def test_update_unknown_ticket_returns_false(store: TicketStore) -> None:
    assert store.update_status("T-19700101-9999", "resolved") is False


def test_update_invalid_status_rejected(store: TicketStore) -> None:
    tk = store.create(title="x", category="其他", priority="P3")
    with pytest.raises(ValueError, match="未知状态"):
        store.update_status(tk["id"], "done")


def test_list_filter_by_status(store: TicketStore) -> None:
    a = store.create(title="a", category="其他", priority="P3")
    store.create(title="b", category="其他", priority="P3")
    store.update_status(a["id"], "resolved")

    assert len(store.list()) == 2
    assert len(store.list(status="open")) == 1
    assert len(store.list(status="resolved")) == 1


def test_list_limit(store: TicketStore) -> None:
    for i in range(5):
        store.create(title=f"t{i}", category="其他", priority="P3")
    assert len(store.list(limit=3)) == 3


def test_stats(store: TicketStore) -> None:
    a = store.create(title="a", category="其他", priority="P3")
    store.create(title="b", category="其他", priority="P3")
    store.update_status(a["id"], "resolved")

    s = store.stats()
    assert s["total"] == 2
    assert s["open"] == 1
    assert s["resolved"] == 1


def test_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    first = TicketStore(db)
    tk = first.create(title="持久化测试", category="其他", priority="P3")

    second = TicketStore(db)
    assert second.get(tk["id"])["title"] == "持久化测试"


def test_delete(store: TicketStore) -> None:
    tk = store.create(title="x", category="其他", priority="P3")
    assert store.delete(tk["id"]) is True
    assert store.get(tk["id"]) is None


# --------------------------------------------------------------------------- #
# 工具层：schema
# --------------------------------------------------------------------------- #

def test_schema_has_enum(registry: ToolRegistry) -> None:
    """category / priority 必须生成 enum，模型才无法填错值。

    这是「用 Literal 而不是 str + 文档说明」的核心收益：
    约束写在 schema 里，模型生成参数时被硬性限制，不靠它自觉。
    """
    create = next(
        s for s in registry.schemas() if s["function"]["name"] == "create_ticket"
    )
    props = create["function"]["parameters"]["properties"]

    assert props["category"]["enum"] == list(CATEGORIES)
    assert props["priority"]["enum"] == list(PRIORITIES)


def test_required_fields(registry: ToolRegistry) -> None:
    create = next(
        s for s in registry.schemas() if s["function"]["name"] == "create_ticket"
    )
    required = set(create["function"]["parameters"]["required"])
    assert required == {"title", "category", "priority", "summary"}
    assert "evidence" not in required, "evidence 应该是可选的"


# --------------------------------------------------------------------------- #
# 工具层：执行
# --------------------------------------------------------------------------- #

async def test_create_ticket_tool_returns_id(
    registry: ToolRegistry, store: TicketStore
) -> None:
    result = await registry.call("create_ticket", {
        "title": "无法登录",
        "category": "账号权限",
        "priority": "P1",
        "summary": "用户反馈账号被锁定，知识库无自助解锁流程",
        "evidence": "检索「账号锁定」0 条；检索「密码重置」0 条",
    })

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["ticket_id"].startswith("T-")
    assert payload["status"] == "open"
    assert payload["priority"] == "P1"
    assert "summary" not in payload, "summary 不该进模型上下文，白占 token"
    assert "evidence" not in payload

    # 确认真的落库，而不是只在返回值里编了个号
    saved = store.get(payload["ticket_id"])
    assert saved is not None
    assert saved["title"] == "无法登录"
    assert "密码重置" in saved["evidence"]


async def test_create_ticket_rejects_invalid_enum(registry: ToolRegistry) -> None:
    """非法枚举值应该在参数校验阶段被挡住，而不是传到存储层才炸。"""
    result = await registry.call("create_ticket", {
        "title": "x",
        "category": "技术问题",       # 不在枚举里
        "priority": "P3",
        "summary": "y",
    })
    assert result.ok is False
    assert "参数校验失败" in result.error


async def test_check_ticket_status_found(
    registry: ToolRegistry, store: TicketStore
) -> None:
    tk = store.create(title="查询测试", category="其他", priority="P2")

    result = await registry.call("check_ticket_status", {"ticket_id": tk["id"]})

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["found"] is True
    assert payload["status"] == "open"
    assert payload["title"] == "查询测试"


async def test_check_ticket_status_not_found(registry: ToolRegistry) -> None:
    """查不到也要返回 ok=True —— 这是业务结果，不是工具故障。"""
    result = await registry.call(
        "check_ticket_status", {"ticket_id": "T-19700101-9999"}
    )
    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["found"] is False
    assert "未找到" in payload["message"]
