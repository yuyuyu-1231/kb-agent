"""tests/test_tools_misc.py — 无参工具与通用工具。

重点守两件事：

1. **无参工具能正常注册**
   之前 `ToolRegistry.register` 里有一句 `if not fields: raise`，
   导致任何没有参数的函数都不能注册成工具。加 `list_documents` /
   `current_time` 时必须放开这个限制。

2. **空参数的 schema 形状正确**
   必须是 `{"type": "object", "properties": {}, "required": []}`，
   而不是 `{"type": "object"}` 或 `{"type": "object", "properties": null}`。
   部分网关对缺失 `properties` 的 schema 会报错。
"""

from __future__ import annotations

import json
from typing import Any

from app.tools.knowledge import register_knowledge_tools
from app.tools.misc import register_misc_tools
from harness.tools import ToolRegistry


class _FakeStore:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def list_documents(self) -> list[dict[str, Any]]:
        return self._docs


class _FakeRetriever:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.store = _FakeStore(docs or [])


# --------------------------------------------------------------------------- #
# 无参工具的注册与 schema
# --------------------------------------------------------------------------- #

def test_register_zero_arg_tool() -> None:
    registry = ToolRegistry()
    register_misc_tools(registry)

    assert registry.names() == ["current_time"]

    spec = registry.schemas()[0]["function"]
    assert spec["name"] == "current_time"
    # 空 properties 是显式的空 dict，不是 None、也不能缺这个键
    assert spec["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_zero_arg_tool_executes() -> None:
    registry = ToolRegistry()
    register_misc_tools(registry)

    import asyncio
    result = asyncio.run(registry.call("current_time", {}))

    assert result.ok is True
    payload = json.loads(result.content)
    assert len(payload["date"]) == 10          # YYYY-MM-DD
    assert isinstance(payload["year"], int)
    assert payload["weekday"].startswith("周")


def test_zero_arg_tool_ignores_extra_args() -> None:
    """模型可能多传参数进来 —— pydantic 空模型默认忽略，不该报错。"""
    registry = ToolRegistry()
    register_misc_tools(registry)

    import asyncio
    result = asyncio.run(registry.call("current_time", {"unexpected": 1}))

    assert result.ok is True


# --------------------------------------------------------------------------- #
# list_documents
# --------------------------------------------------------------------------- #

async def test_list_documents_shapes_result() -> None:
    retriever = _FakeRetriever([
        {
            "doc_id": "员工手册",
            "doc_name": "员工手册.pdf",
            "page_count": 6,
            "chunk_count": 14,
            # content_hash 不该出现在工具返回里：模型不需要，白占 token
            "content_hash": "abc123",
        },
    ])
    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)

    result = await registry.call("list_documents", {})

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload == [
        {"doc_id": "员工手册", "doc_name": "员工手册.pdf",
         "page_count": 6, "chunk_count": 14}
    ]


async def test_list_documents_empty_store() -> None:
    registry = ToolRegistry()
    register_knowledge_tools(registry, _FakeRetriever([]))

    result = await registry.call("list_documents", {})

    assert result.ok is True
    assert json.loads(result.content) == []
