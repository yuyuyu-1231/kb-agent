"""harness/tools.py 的单元测试。

原则：全部 mock，不接 LLM、不接 Qdrant、不读磁盘。
内核必须能脱离一切外部服务独立验证，否则第 1 周就会被环境问题卡住。
"""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from harness.tools import ToolRegistry, _parse_docstring


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool
    async def search_knowledge(
        query: str,
        top_k: int = 5,
        doc_name: str | None = None,
    ) -> list[dict]:
        """检索知识库中的相关片段。

        Args:
            query: 自然语言查询
            top_k: 返回条数
            doc_name: 限定文档名，可选
        """
        return [{"text": f"命中:{query}", "page": 1}][:top_k]

    @reg.tool
    def sync_echo(msg: str) -> dict:
        """同步工具，用于验证同步函数走线程池。

        Args:
            msg: 任意消息
        """
        return {"echo": msg}

    return reg


def _tool_schema(registry: ToolRegistry, name: str) -> dict:
    return next(s for s in registry.schemas() if s["function"]["name"] == name)


# --------------------------------------------------------------------------- #
# Schema 生成
# --------------------------------------------------------------------------- #

def test_schema_shape(registry: ToolRegistry) -> None:
    """schema 必须符合 OpenAI tools 格式，且类型/默认值/描述都正确。"""
    fn = _tool_schema(registry, "search_knowledge")["function"]

    assert fn["name"] == "search_knowledge"
    assert "检索知识库" in fn["description"]

    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["query"], "只有无默认值的参数才是 required"

    props = params["properties"]
    assert props["query"]["type"] == "string"
    assert props["query"]["description"] == "自然语言查询"
    assert props["top_k"]["type"] == "integer"
    assert props["top_k"]["default"] == 5
    # Optional[str] 会被 pydantic 展开成 anyOf [string, null]
    assert "anyOf" in props["doc_name"]


def test_schema_has_no_titles(registry: ToolRegistry) -> None:
    """pydantic 的 title 字段必须被剥掉，否则白白消耗 tools 参数的 token。"""
    for schema in registry.schemas():
        assert "'title'" not in str(schema)


def test_literal_inlined() -> None:
    """Literal 应内联成 enum，schema 中不得残留 $ref/$defs。"""
    reg = ToolRegistry()

    @reg.tool
    async def make_ticket(title: str, category: Literal["故障报修", "账号权限"]) -> str:
        """创建工单。

        Args:
            title: 工单标题
            category: 工单分类
        """
        return "ok"

    params = _tool_schema(reg, "make_ticket")["function"]["parameters"]
    assert params["properties"]["category"]["enum"] == ["故障报修", "账号权限"]
    assert "$ref" not in str(params)
    assert "$defs" not in str(params)


def test_docstring_param_docs_merged(registry: ToolRegistry) -> None:
    """docstring 的 Args 段必须被合并进每个参数的 description。"""
    props = _tool_schema(registry, "search_knowledge")["function"]["parameters"]["properties"]
    assert props["top_k"]["description"] == "返回条数"
    assert props["doc_name"]["description"] == "限定文档名，可选"


def test_parse_docstring() -> None:
    doc = """摘要第一行。
    摘要第二行。

    Args:
        a: 参数A
        b (int): 参数B

    Returns:
        结果
    """
    summary, params = _parse_docstring(doc)
    assert "摘要第一行" in summary
    assert "Returns" not in summary
    assert params == {"a": "参数A", "b": "参数B"}


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #

def test_duplicate_registration_raises(registry: ToolRegistry) -> None:
    def dup(query: str) -> str:
        """重复注册。

        Args:
            query: 查询
        """
        return ""

    with pytest.raises(ValueError, match="重复注册"):
        registry.register(dup, name="search_knowledge")


def test_paramless_tool_registers() -> None:
    """无参工具是合法的。

    这条原来叫 test_paramless_tool_rejected，断言“没有参数就 raise”。
    加 `list_documents` / `current_time` 时放开了这个限制——
    一个不需要参数的函数完全可以是个好工具（“列出所有文档”就不需要参数）。

    现在守的是另一件事：**空参数的 schema 形状必须正确**，
    必须是 `{"type": "object", "properties": {}, "required": []}`，
    而不是缺 `properties` 键或者值为 None —— 部分网关会因此报错。
    """
    reg = ToolRegistry()

    def no_params() -> str:
        """没有参数。"""
        return ""

    spec = reg.register(no_params)

    assert spec.name == "no_params"
    assert spec.parameters == {
        "type": "object",
        "properties": {},
        "required": [],
    }
    # 注册后能被调用（空参数）
    assert "no_params" in reg


# --------------------------------------------------------------------------- #
# 执行：正常路径
# --------------------------------------------------------------------------- #

async def test_async_tool_ok(registry: ToolRegistry) -> None:
    result = await registry.call("search_knowledge", {"query": "年假"})
    assert result.ok is True
    assert result.latency_ms >= 0


async def test_sync_tool_runs_in_thread(registry: ToolRegistry) -> None:
    """同步工具必须能正常调用（内部走 asyncio.to_thread）。"""
    result = await registry.call("sync_echo", {"msg": "你好"})
    assert result.ok is True
    assert "你好" in result.content


async def test_chinese_not_escaped(registry: ToolRegistry) -> None:
    """返回的 JSON 必须保留中文，不能变成 \\uXXXX。"""
    result = await registry.call("search_knowledge", {"query": "年假"})
    assert "年假" in result.content
    assert "\\u" not in result.content


async def test_default_applied(registry: ToolRegistry) -> None:
    """未传的可选参数必须使用默认值，不能报缺参。"""
    result = await registry.call("search_knowledge", {"query": "考勤"})
    assert result.ok is True


# --------------------------------------------------------------------------- #
# 执行：失败路径（核心设计点）
# --------------------------------------------------------------------------- #

async def test_invalid_args_returns_error_not_raises(registry: ToolRegistry) -> None:
    """参数不合法时必须返回 ok=False，而不是抛异常。

    这是 Agent 自愈的前提：loop 拿到错误文本后回喂模型，模型下一轮自己改。
    """
    result = await registry.call("search_knowledge", {"query": "年假", "top_k": "abc"})
    assert result.ok is False
    assert "参数校验失败" in result.error
    # 错误必须能转成可回喂的消息
    assert "error" in result.to_message_content()


async def test_missing_required_arg(registry: ToolRegistry) -> None:
    result = await registry.call("search_knowledge", {})
    assert result.ok is False
    assert "参数校验失败" in result.error


async def test_unknown_tool(registry: ToolRegistry) -> None:
    result = await registry.call("not_exist", {})
    assert result.ok is False
    assert "未注册的工具" in result.error


async def test_tool_exception_captured() -> None:
    reg = ToolRegistry()

    @reg.tool
    async def boom(x: int) -> str:
        """总是抛异常。

        Args:
            x: 任意数字
        """
        raise RuntimeError("故意炸")

    result = await reg.call("boom", {"x": 1})
    assert result.ok is False
    assert "RuntimeError" in result.error
    assert "故意炸" in result.error


async def test_timeout_captured() -> None:
    reg = ToolRegistry()

    @reg.tool(timeout=0.05)
    async def slow(x: int) -> str:
        """睡很久。

        Args:
            x: 秒数
        """
        await asyncio.sleep(1)
        return "done"

    result = await reg.call("slow", {"x": 1})
    assert result.ok is False
    assert "超时" in result.error


def test_error_message_is_json() -> None:
    """失败内容要序列化成 JSON，模型才能结构化理解错误。"""
    from harness.tools import ToolResult

    r = ToolResult(ok=False, error="参数校验失败: x", meta={"tool": "t"})
    content = r.to_message_content()
    assert content.startswith("{")
    assert "参数校验失败" in content
