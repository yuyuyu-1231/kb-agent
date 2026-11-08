"""harness/tools.py — 工具注册与 JSON Schema 自动生成。

这个模块解决 Function Calling 落地的第一个问题：
模型怎么知道有哪些工具、参数长什么样、传错了怎么办。

三个设计要点（面试可讲）：
1. Schema 不手写，从函数签名 + 类型注解 + docstring 自动生成。
   手写 schema 和函数签名必然长期不一致，这是维护陷阱。
2. 参数校验失败不抛异常，而是返回结构化错误，让 Agent 循环把它作为
   role=tool 消息回喂给模型，模型下一轮自己修正 —— 这是 Agent 自愈的来源。
3. 工具异常、超时也走同一条返回路径，调用方(loop) 不需要 try/except。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import typing
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError, create_model


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class ToolResult:
    """工具调用的统一返回。成功、参数错、异常、超时，都返回这个结构。

    content 永远是字符串，因为 OpenAI 协议里 tool 消息的 content 必须是字符串。
    """
    ok: bool
    content: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def to_message_content(self) -> str:
        """转成喂回模型的 role=tool 消息内容。

        错误也返回文本，不返回空串 —— 模型需要看到"为什么失败"才能修正。
        这是 Agent 自我纠错的前提，也是和"工具挂了就让用户看 traceback"的本质区别。
        """
        if self.ok:
            return self.content
        return json.dumps(
            {"error": self.error, "meta": self.meta},
            ensure_ascii=False,
        )


@dataclass
class ToolSpec:
    """一个工具的完整定义。"""
    name: str
    description: str
    parameters: dict[str, Any]        # 清洗后的 JSON Schema
    func: Callable[..., Any]
    validator: type[BaseModel]        # 动态生成的 pydantic 模型
    timeout: float = 30.0
    is_async: bool = False
    # 返回的列表是否应当被当作「引用候选」：loop 会给每条分配全局稳定编号。
    # 用它而不是在 loop 里硬编码工具名，避免内核层依赖应用层的命名。
    citation_aware: bool = False


# --------------------------------------------------------------------------- #
# Schema 工具函数
# --------------------------------------------------------------------------- #

def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 pydantic 产生的 $defs/$ref 展开成内联定义。

    原因：JSON Schema 的 $ref 在部分模型/网关里支持不完整，会被当成未知字段
    静默忽略，导致参数定义丢失、模型瞎填。内联最保险。

    例如 Enum 会生成：
        {"$defs": {"Priority": {"enum": [...], "title": "Priority"}},
         "properties": {"p": {"$ref": "#/$defs/Priority"}}}
    展开后：
        {"properties": {"p": {"enum": [...], "type": "string"}}}
    """
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).split("/")[-1]
                target = resolve(defs.get(ref_name, {}))
                # $ref 同级键（如 description）优先级更高
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def _strip_titles(node: Any) -> Any:
    """去掉 pydantic 自动加的 "title" 字段。

    title 只是把 snake_case 转成 Title Case，对模型没有信息量，
    但会显著增加 tools 参数的 token 开销。工具一多这部分是纯浪费。
    """
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(x) for x in node]
    return node


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """从 Google 风格 docstring 中拆出「工具描述」和「参数描述」。

    为什么参数描述重要：模型选工具看工具描述，填参数看参数描述。
    参数写不清楚，模型就会填错（top_k / page_size 这类混淆很常见），
    而且靠 prompt 改不动，只能靠 schema 说清楚。
    """
    if not doc:
        return "", {}

    lines = doc.strip().splitlines()
    summary: list[str] = []
    param_docs: dict[str, str] = {}
    in_args = False

    for line in lines:
        stripped = line.strip()

        if stripped in ("Args:", "Arguments:", "参数:"):
            in_args = True
            continue
        if stripped in ("Returns:", "Raises:", "Yields:", "返回:", "异常:"):
            in_args = False
            continue

        if in_args:
            if ":" in stripped:
                key, _, desc = stripped.partition(":")
                key = key.strip()
                desc = desc.strip()
                # 兼容 "query (str): ..." 这种带类型标注的写法
                if " " in key:
                    key = key.split(" ")[0]
                if key and desc:
                    param_docs[key] = desc
        elif stripped:
            summary.append(stripped)

    return "\n".join(summary).strip(), param_docs


def _to_content(ret: Any) -> str:
    """把工具返回值统一转成字符串。

    OpenAI 协议要求 tool 消息 content 为字符串。返回 dict/list 时用 JSON 序列化，
    并保留中文(ensure_ascii=False)，否则中文变成 \\uXXXX，模型读起来更费 token。
    """
    if isinstance(ret, str):
        return ret
    try:
        return json.dumps(ret, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(ret)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

class ToolRegistry:
    """工具注册表：注册 -> 生成 schema -> 执行。

    典型用法：

        registry = ToolRegistry()

        @registry.tool(timeout=15)
        async def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
            '''检索知识库。

            Args:
                query: 自然语言查询
                top_k: 返回条数
            '''
            ...

        tools = registry.schemas()                       # 喂给 LLM
        result = await registry.call("search_knowledge", {"query": "年假"})
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ---------------------------------------------------------------- 注册

    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        timeout: float = 30.0,
        citation_aware: bool = False,
    ) -> Any:
        """装饰器。同时支持 @registry.tool 和 @registry.tool(timeout=15)。"""
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(func, name=name, timeout=timeout,
                          citation_aware=citation_aware)
            return func

        if fn is not None:
            return decorator(fn)
        return decorator

    def register(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        timeout: float = 30.0,
        citation_aware: bool = False,
    ) -> ToolSpec:
        """解析函数签名，生成 JSON Schema，登记到表中。"""
        tool_name = name or fn.__name__

        if tool_name in self._tools:
            raise ValueError(f"工具名重复注册: {tool_name}")

        sig = inspect.signature(fn)
        # get_type_hints 会解析字符串注解（from __future__ import annotations 场景）
        try:
            hints = typing.get_type_hints(fn)
        except Exception:
            hints = {}

        fields: dict[str, tuple[Any, Any]] = {}
        for pname, param in sig.parameters.items():
            if pname in ("self", "cls") or pname.startswith("_"):
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                # v1 不支持 *args / **kwargs：JSON Schema 表达不了，模型也用不上
                continue

            annotation = hints.get(pname, str)
            default = param.default if param.default is not inspect.Parameter.empty else ...
            fields[pname] = (annotation, default)

        # 无参工具是合法的（例如“列出所有文档”“获取当前时间”）。
        # 不要在这里 raise —— 一个不需要参数的函数完全可以是一个好工具。
        validator = create_model(f"{tool_name}_Args", **fields)

        description, param_docs = _parse_docstring(inspect.getdoc(fn))

        if fields:
            # 用 pydantic 动态建模型：注解解析、Optional、Literal、Enum、默认值
            # 全部交给 pydantic，比自己遍历 typing 分支可靠得多
            raw_schema = validator.model_json_schema()
            raw_schema = _inline_refs(raw_schema)
            raw_schema = _strip_titles(raw_schema)
            props = raw_schema.get("properties", {})
            required = raw_schema.get("required", [])
        else:
            # 空模型不生成 properties，手写一个空 object
            props = {}
            required = []

        # 把 docstring 里的参数说明补进 schema
        for pname, pdesc in param_docs.items():
            if pname in props:
                props[pname]["description"] = pdesc

        parameters = {
            "type": "object",
            "properties": props,
            "required": required,
        }

        spec = ToolSpec(
            name=tool_name,
            description=description,
            parameters=parameters,
            func=fn,
            validator=validator,
            timeout=timeout,
            is_async=inspect.iscoroutinefunction(fn),
            citation_aware=citation_aware,
        )
        self._tools[tool_name] = spec
        return spec

    # ---------------------------------------------------------------- 查询

    def schemas(self) -> list[dict[str, Any]]:
        """生成 OpenAI tools 参数。

        这个结构会塞进 chat.completions.create(tools=...)，
        模型看到的就是这里的 name / description / parameters。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ---------------------------------------------------------------- 执行

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        """执行工具。所有失败路径都返回 ToolResult(ok=False)，绝不抛异常。

        这是整个设计的关键：Agent 循环不能因为一个工具崩了就整体崩。
        失败信息要作为 observation 回喂给模型，让它有机会换参数、换工具，
        或者承认失败转入转工单流程。
        """
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                error=f"未注册的工具: {name}。可用工具: {', '.join(self._tools)}",
                latency_ms=elapsed_ms(),
            )

        # 1) 参数校验：失败时把 pydantic 原文回喂，模型能看懂哪个字段错、要什么类型
        try:
            validated = spec.validator(**args)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                error=f"参数校验失败:\n{exc}",
                meta={"tool": name, "raw_args": args},
                latency_ms=elapsed_ms(),
            )
        except TypeError as exc:
            # args 不是 dict，或字段名完全对不上
            return ToolResult(
                ok=False,
                error=f"参数无法绑定: {exc}",
                meta={"tool": name, "raw_args": args},
                latency_ms=elapsed_ms(),
            )

        kwargs = validated.model_dump()

        # 2) 执行：同步函数丢线程池，避免阻塞事件循环
        try:
            if spec.is_async:
                ret = await asyncio.wait_for(spec.func(**kwargs), timeout=spec.timeout)
            else:
                ret = await asyncio.wait_for(
                    asyncio.to_thread(spec.func, **kwargs),
                    timeout=spec.timeout,
                )
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                error=f"工具 {name} 超时(>{spec.timeout}s)",
                meta={"tool": name},
                latency_ms=elapsed_ms(),
            )
        except Exception as exc:  # noqa: BLE001 - 工具内部任何异常都要兜住
            return ToolResult(
                ok=False,
                error=f"工具 {name} 执行异常: {type(exc).__name__}: {exc}",
                meta={"tool": name},
                latency_ms=elapsed_ms(),
            )

        return ToolResult(
            ok=True,
            content=_to_content(ret),
            meta={"tool": name},
            latency_ms=elapsed_ms(),
        )
