"""app/infra/llm.py — 把 openai SDK 适配成 harness 的 LLMClient 协议。

这一层存在的唯一理由：**把第三方 SDK 的形状挡在核心里**。

harness/loop.py 只认 `async chat(messages, tools) -> LLMResponse`，
不知道也不关心底层是 OpenAI、DeepSeek 还是别的网关。换模型时只改这个文件。

三个必须处理的现实问题：

1. **arguments 是 JSON 字符串，不是对象**
   模型返回的 `function.arguments` 是一段字符串（"{...}"），必须先 json.loads。
   但这段字符串可能不合法（截断、多了逗号、模型幻觉出非法 JSON），
   所以解析失败时返回空 dict 并保留 raw_arguments —— 让 ToolRegistry 的
   参数校验失败，错误再回喂给模型，模型下一轮能改。这是自愈链路的一环。

2. **usage 可能为 None**
   部分 OpenAI 兼容网关在流式或某些模式下不返回 usage。
   直接取 `resp.usage.prompt_tokens` 会 AttributeError。

3. **content 与 tool_calls 可以同时存在**
   实测 DeepSeek 在返回 tool_calls 的同时会给一段自然语言（"我将检索知识库…"）。
   OpenAI 通常是 content=None。两种情况都要保留，不能丢信息。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from harness.loop import LLMDelta, LLMResponse, ToolCall


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """把 function.arguments 解析成 dict。

    解析失败返回空 dict —— 这不是"吞掉错误"，而是把错误交给下一层：
    ToolRegistry 会用空参数去校验，必然缺必填字段，于是产出一条
    "参数校验失败，缺少 query" 的 observation 回喂给模型，模型下一轮能改对。
    比在这里直接抛异常好，因为抛异常会中断整个 run。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAILLMClient:
    """实现了 harness.loop.LLMClient 协议。

    用法：

        from app.core.config import get_settings
        client = OpenAILLMClient.from_settings(get_settings())
        loop = AgentLoop(llm=client, registry=registry, ...)
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise ValueError("LLM_API_KEY 为空，检查 .env")

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=2,          # 网络抖动时自动重试，重试由 SDK 负责
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenAILLMClient":
        return cls(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model_id,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

    # ---------------------------------------------------------------- 协议

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            # 显式要求模型调用工具，否则它可能选择直接回答
            kwargs["tool_choice"] = "auto"
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        resp = await self._client.chat.completions.create(**kwargs)
        return self._to_response(resp)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMDelta]:
        """流式调用。逐块产出文本，tool_calls 在末尾一次性给出。

        三个必须处理的坑：

        1. **tool_calls 是分片到达的**
           第一块只有 id 和 name，后续块只有 arguments 的片段。
           必须按 index 分开累积，最后拼成一份完整的 JSON 字符串。
           这是 SDK 细节，不能泄到内核。

        2. **usage 只在最后一个 chunk 出现**
           而且要显式传 `stream_options={"include_usage": True}`
           才会返回。不传的话 token 统计全是 0，评估就废了。

        3. **有些 chunk 的 choices 是空列表**
           带 usage 的那个 chunk 往往就没有 choices。
           不判空会在 `chunk.choices[0]` 上 IndexError。
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        stream = await self._client.chat.completions.create(**kwargs)

        # index -> {id, name, args}，按工具序号分开累积
        buffers: dict[int, dict[str, str]] = {}
        finish_reason = ""
        prompt_tokens = 0
        completion_tokens = 0

        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            choice = choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            if delta.content:
                yield LLMDelta(content=delta.content)

            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = tc.index if tc.index is not None else 0
                slot = buffers.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if fn.name:
                        slot["name"] = fn.name
                    if fn.arguments:
                        slot["args"] += fn.arguments

        calls = [
            ToolCall(
                id=slot["id"] or f"call_{idx}",
                name=slot["name"],
                arguments=_parse_arguments(slot["args"]),
                raw_arguments=slot["args"],
            )
            for idx, slot in sorted(buffers.items())
        ]

        yield LLMDelta(
            tool_calls=calls,
            finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # ---------------------------------------------------------------- 转换

    @staticmethod
    def _to_response(resp: Any) -> LLMResponse:
        """SDK 响应 -> 中立数据结构。"""
        choice = resp.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        for tc in (message.tool_calls or []):
            raw = tc.function.arguments
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=_parse_arguments(raw),
                raw_arguments=raw or "",
            ))

        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接。FastAPI 的 lifespan 里调用。"""
        await self._client.close()
