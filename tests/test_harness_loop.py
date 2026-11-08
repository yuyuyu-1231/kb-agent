"""harness/loop.py 的单元测试。

关键：全部使用 FakeLLM，不连任何真实 API。这正是 loop.py 只依赖
LLMClient 协议（而不是 openai SDK）的价值——内核完全脱网可测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.guard import StopReason
from harness.loop import AgentLoop, LLMDelta, LLMResponse, ToolCall
from harness.memory import SessionMemory
from harness.tools import ToolRegistry
from harness.trace import replay


# --------------------------------------------------------------------------- #
# 替身：假 LLM
# --------------------------------------------------------------------------- #

class FakeLLM:
    """按顺序吐出预设响应，并记录每次收到的 messages。"""

    def __init__(self, *responses: LLMResponse) -> None:
        self._queue = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
        })
        if not self._queue:
            raise AssertionError("FakeLLM 的预设响应已用完")
        return self._queue.pop(0)

    async def chat_stream(self, messages, tools=None):
        """把预设响应切成流。

        测试只关心「内容能不能拼回来」，不模拟真实网络分片。
        按 3 字符一块切开就够验证 loop 的拼接逻辑了。

        注意：内部调 self.chat，所以 calls 的记录行为与之前一致，
        现有断言 llm.calls 的测试不需要改。
        """
        resp = await self.chat(messages, tools)
        if resp.content:
            for i in range(0, len(resp.content), 3):
                yield LLMDelta(content=resp.content[i:i + 3])
        yield LLMDelta(
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
        )


class BoomLLM:
    async def chat(self, messages, tools=None):
        raise ConnectionError("模拟网络中断")

    async def chat_stream(self, messages, tools=None):
        raise ConnectionError("模拟网络中断")
        yield  # pragma: no cover - 让这个函数成为 async generator


def text(content: str, tokens: int = 10) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop",
                       prompt_tokens=tokens, completion_tokens=tokens)


def call(name: str, args: dict, call_id: str = "call_1", tokens: int = 10) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args,
                             raw_arguments=json.dumps(args, ensure_ascii=False))],
        finish_reason="tool_calls",
        prompt_tokens=tokens,
        completion_tokens=tokens,
    )


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(citation_aware=True)
    async def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
        """检索知识库。

        Args:
            query: 自然语言查询
            top_k: 返回条数
        """
        return [{
            "text": f"关于{query}的规定：满一年 5 天",
            "doc_id": "doc-1",
            "doc_name": "员工手册.pdf",
            "page_no": 12,
            "section_title": "第三章 假期制度",
            "chunk_index": 0,
            "score": 0.83,
        }]

    @reg.tool
    async def create_ticket(title: str, category: str, priority: str) -> dict:
        """创建工单。

        Args:
            title: 工单标题
            category: 分类
            priority: 优先级
        """
        return {"ticket_id": "T-001"}

    return reg


def make_loop(llm, registry: ToolRegistry, **kwargs) -> AgentLoop:
    return AgentLoop(llm=llm, registry=registry, **kwargs)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

async def test_answer_without_tools(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("不需要查资料，直接回答"))
    loop = make_loop(llm, registry)

    result = await loop.run("你好")

    assert result.stop_reason is StopReason.ANSWERED
    assert result.answer == "不需要查资料，直接回答"
    assert result.steps == 1
    assert result.citations == []


async def test_single_tool_then_answer(registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("根据[1]，满一年有 5 天年假。"),
    )
    loop = make_loop(llm, registry)

    result = await loop.run("试用期能休年假吗")

    assert result.stop_reason is StopReason.ANSWERED
    assert result.steps == 2
    assert len(result.citations) == 1
    assert result.citations[0].page_no == 12
    assert result.citations[0].doc_name == "员工手册.pdf"
    assert result.citations[0].index == 1


async def test_multi_round_preserves_message_order(registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        call("search_knowledge", {"query": "试用期 年假"}, call_id="call_2"),
        text("综合两条资料[1][2]"),
    )
    loop = make_loop(llm, registry)

    result = await loop.run("问题")

    assert result.stop_reason is StopReason.ANSWERED
    assert result.steps == 3

    # 第三次调用时，历史里应该已经有 assistant(tool_calls) + tool 各两条
    third = llm.calls[2]["messages"]
    roles = [m["role"] for m in third]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]


async def test_tools_schema_passed_to_llm(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("ok"))
    loop = make_loop(llm, registry)
    await loop.run("问题")

    tools = llm.calls[0]["tools"]
    names = [t["function"]["name"] for t in tools]
    assert names == ["search_knowledge", "create_ticket"]


async def test_custom_system_prompt(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("ok"))
    loop = make_loop(llm, registry, system_prompt="你是测试助手")
    await loop.run("问题")

    assert llm.calls[0]["messages"][0] == {"role": "system", "content": "你是测试助手"}


async def test_empty_system_prompt_omitted(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("ok"))
    loop = make_loop(llm, registry, system_prompt="")
    await loop.run("问题")

    assert llm.calls[0]["messages"][0]["role"] == "user"


# --------------------------------------------------------------------------- #
# 工单路径
# --------------------------------------------------------------------------- #

async def test_ticket_created(registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("create_ticket", {"title": "年假咨询", "category": "产品功能", "priority": "P2"}),
        text("已为你创建工单，稍后人工跟进。"),
    )
    loop = make_loop(llm, registry)

    result = await loop.run("我要投诉")

    assert result.stop_reason is StopReason.TICKET_CREATED
    assert result.ticket_id == "T-001"


# --------------------------------------------------------------------------- #
# 三重熔断
# --------------------------------------------------------------------------- #

async def test_max_steps_trips(registry: ToolRegistry) -> None:
    """每轮参数都不同，避免触发重复动作检测，从而走到轮数熔断。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "q1"}),
        call("search_knowledge", {"query": "q2"}, call_id="call_2"),
    )
    loop = make_loop(llm, registry, max_steps=2)

    result = await loop.run("问题")

    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.answer                      # 必须有降级文案
    assert "轮数" in result.answer or "工单" in result.answer


async def test_budget_exhausted(registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("search_knowledge", {"query": "q1"}, tokens=500),
        call("search_knowledge", {"query": "q2"}, call_id="call_2", tokens=500),
    )
    loop = make_loop(llm, registry, token_budget=100)

    result = await loop.run("问题")

    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.answer


async def test_loop_detected(registry: ToolRegistry) -> None:
    """连续两次完全相同的工具调用，第二次熔断。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        call("search_knowledge", {"query": "年假"}, call_id="call_2"),
    )
    loop = make_loop(llm, registry)

    result = await loop.run("问题")

    assert result.stop_reason is StopReason.LOOP_DETECTED
    assert result.answer


async def test_llm_error(registry: ToolRegistry) -> None:
    loop = make_loop(BoomLLM(), registry)

    result = await loop.run("问题")

    assert result.stop_reason is StopReason.LLM_ERROR
    assert "模型服务" in result.answer
    assert "ConnectionError" in result.detail


# --------------------------------------------------------------------------- #
# 工具失败回喂
# --------------------------------------------------------------------------- #

async def test_invalid_args_fed_back_and_model_recovers(registry: ToolRegistry) -> None:
    """参数错 -> 错误回喂 -> 模型下一轮改对。这是 Agent 自愈的完整链路。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假", "top_k": "abc"}),   # 故意传错
        call("search_knowledge", {"query": "年假", "top_k": 3}, call_id="call_2"),
        text("根据[1]，年假 5 天。"),
    )
    loop = make_loop(llm, registry)

    result = await loop.run("年假几天")

    assert result.stop_reason is StopReason.ANSWERED

    # 第二次调用时，历史里应该有一条带错误信息的 tool 消息
    second_call_messages = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_call_messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "参数校验失败" in tool_msgs[0]["content"]


async def test_tool_pairing_stays_valid(registry: ToolRegistry) -> None:
    """每轮结束后 tool_calls 与 tool 消息必须一一对应，否则真实 API 会 400。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("答案"),
    )
    loop = make_loop(llm, registry)
    await loop.run("问题")

    messages = llm.calls[1]["messages"]
    call_ids = {tc["id"] for m in messages for tc in (m.get("tool_calls") or [])}
    tool_ids = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
    assert call_ids == tool_ids


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #

async def test_trace_written(tmp_path: Path, registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("答案[1]"),
    )
    loop = make_loop(
        llm, registry,
        trace_dir=tmp_path / "traces",
        artifact_dir=tmp_path / "artifacts",
    )

    result = await loop.run("问题")

    records = replay(tmp_path / "traces", result.trace_id,
                     artifact_dir=tmp_path / "artifacts")
    assert records[0]["kind"] == "step"
    assert records[-1]["kind"] == "final"
    assert records[-1]["stop_reason"] == "answered"

    tool_steps = [r for r in records if r.get("tool") == "search_knowledge"]
    assert len(tool_steps) == 1
    assert tool_steps[0]["ok"] is True
    assert tool_steps[0]["observation"]          # artifact 已恢复全文


async def test_trace_disabled_when_no_dir(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("ok"))
    loop = make_loop(llm, registry)
    result = await loop.run("问题")
    assert result.trace_id                      # 仍然有 id，只是不落盘


async def test_trace_records_context_trimming(tmp_path: Path, registry: ToolRegistry) -> None:
    """预算极小时，context 裁剪动作要被记进 trace。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "x" * 500}),
        text("ok"),
    )
    loop = make_loop(
        llm, registry,
        token_budget=50,
        trace_dir=tmp_path / "traces",
        artifact_dir=tmp_path / "artifacts",
    )
    result = await loop.run("问题")

    records = replay(tmp_path / "traces", result.trace_id)
    trimmed = [r for r in records if "[context]" in str(r.get("thought", ""))]
    assert trimmed, "裁剪动作必须留下痕迹"


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #

async def test_memory_written_on_success(tmp_path: Path, registry: ToolRegistry) -> None:
    mem = SessionMemory(tmp_path / "s.db", keep_turns=5)
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("年假 5 天[1]"),
    )
    loop = make_loop(llm, registry, memory=mem)

    await loop.run("年假几天", session_id="s1")

    history = mem.load("s1")
    assert len(history) == 2
    assert history[0]["content"] == "年假几天"
    assert history[1]["role"] == "assistant"


async def test_memory_not_written_on_failure(tmp_path: Path, registry: ToolRegistry) -> None:
    """熔断的对话质量低，写进记忆会污染下一轮上下文。"""
    mem = SessionMemory(tmp_path / "s.db", keep_turns=5)
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        call("search_knowledge", {"query": "年假"}, call_id="call_2"),
    )
    loop = make_loop(llm, registry, memory=mem)

    result = await loop.run("问题", session_id="s1")

    assert result.stop_reason is StopReason.LOOP_DETECTED
    assert mem.load("s1") == []


async def test_history_loaded_into_messages(tmp_path: Path, registry: ToolRegistry) -> None:
    mem = SessionMemory(tmp_path / "s.db", keep_turns=5)
    mem.append("s1",
               {"role": "user", "content": "上一轮问题"},
               {"role": "assistant", "content": "上一轮答案"})

    llm = FakeLLM(text("这一轮答案"))
    loop = make_loop(llm, registry, memory=mem)

    await loop.run("这一轮问题", session_id="s1")

    messages = llm.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "上一轮问题"
    assert messages[3]["content"] == "这一轮问题"


async def test_no_session_id_skips_memory(registry: ToolRegistry) -> None:
    llm = FakeLLM(text("ok"))
    loop = make_loop(llm, registry)
    result = await loop.run("问题")          # 不传 session_id
    assert result.session_id == ""


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

async def test_result_to_dict_serializable(registry: ToolRegistry) -> None:
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("答案[1]"),
    )
    loop = make_loop(llm, registry)
    result = await loop.run("问题")

    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    parsed = json.loads(payload)
    assert parsed["stop_reason"] == "answered"
    assert parsed["citations"][0]["page_no"] == 12


async def test_degrade_text_differs_by_reason(registry: ToolRegistry) -> None:
    """不同熔断原因应给出不同的用户提示，而不是统一一句"失败"。"""
    llm1 = FakeLLM(call("search_knowledge", {"query": "a"}),
                   call("search_knowledge", {"query": "a"}, call_id="c2"))
    r1 = await make_loop(llm1, registry).run("问题")

    llm2 = FakeLLM(BoomLLM and text("x"))
    r2 = await make_loop(BoomLLM(), registry).run("问题")

    assert r1.answer != r2.answer


# --------------------------------------------------------------------------- #
# 协议配对：熔断时必须补齐占位 tool 消息
# --------------------------------------------------------------------------- #

def test_pad_skipped_tool_calls_from_middle() -> None:
    """一批 3 个 tool_calls，从第 2 个开始熔断，应补 2 条占位消息。"""
    calls = [ToolCall(id=f"c{i}", name="t") for i in (1, 2, 3)]
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "t", "arguments": "{}"}}
            for i in (1, 2, 3)
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]

    AgentLoop._pad_skipped_tool_calls(messages, calls, 1)

    tool_ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2", "c3"]
    assert "熔断" in messages[2]["content"]


def test_pad_skipped_tool_calls_from_zero() -> None:
    """第一个就熔断，应补全部。"""
    calls = [ToolCall(id="c1", name="t"), ToolCall(id="c2", name="t")]
    messages: list[dict] = [{"role": "assistant", "content": None, "tool_calls": []}]

    AgentLoop._pad_skipped_tool_calls(messages, calls, 0)

    tool_ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2"]


async def test_halt_mid_batch_keeps_protocol_valid(registry: ToolRegistry) -> None:
    """一次返回 2 个相同参数的 tool_calls，第 2 个触发熔断。

    真实测过：DeepSeek 经常一次返回多个 tool_calls。如果不补占位消息，
    messages 会处于非法状态，一旦重发就是 400。
    """
    resp = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id="c1", name="search_knowledge", arguments={"query": "x"}),
            ToolCall(id="c2", name="search_knowledge", arguments={"query": "x"}),
        ],
        finish_reason="tool_calls",
    )
    llm = FakeLLM(resp)
    result = await make_loop(llm, registry).run("问题")

    assert result.stop_reason is StopReason.LOOP_DETECTED


# --------------------------------------------------------------------------- #
# 引用编号：全局稳定 + 只返回被引用的
# --------------------------------------------------------------------------- #

async def test_tool_result_carries_stable_index(registry: ToolRegistry) -> None:
    """工具返回给模型的内容里必须有 index，且不应暴露 chunk_index。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("答案[1]"),
    )
    loop = make_loop(llm, registry)
    await loop.run("问题")

    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1

    payload = json.loads(tool_msgs[0]["content"])
    assert isinstance(payload, list)
    assert payload[0]["index"] == 1, "模型要能看见 index 才能引用"
    assert "chunk_index" not in payload[0], "chunk_index 不该占模型的 token"
    assert payload[0]["doc_id"] == "doc-1", "doc_id 必须留着，read_document_page 要用"


async def test_citation_index_stable_across_searches(registry: ToolRegistry) -> None:
    """多轮检索到同一个 chunk，编号必须保持为 1。

    回归背景：曾经每次 search_knowledge 都从 1 重编并直接覆盖 citations，
    导致多轮检索时答案里的 [2] 和最终 citations 里的 [2] 指向不同内容。
    """
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        call("search_knowledge", {"query": "年假 细则"}, call_id="c2"),
        text("综合两次检索，依据[1]。"),
    )
    loop = make_loop(llm, registry)
    result = await loop.run("问题")

    assert len(result.citations) == 1, "同一个 chunk 不应重复计入"
    assert result.citations[0].index == 1


async def test_unreferenced_citations_dropped(registry: ToolRegistry) -> None:
    """答案里没引用到的 chunk 不进最终结果。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假"}),
        text("这是一句没有任何编号的答案。"),
    )
    loop = make_loop(llm, registry)
    result = await loop.run("问题")

    assert result.citations == []


async def test_citation_survives_tool_failure(registry: ToolRegistry) -> None:
    """工具失败时不能分配编号，否则 citations 会出现无内容的幽灵条目。"""
    llm = FakeLLM(
        call("search_knowledge", {"query": "年假", "top_k": "abc"}),
        call("search_knowledge", {"query": "年假", "top_k": 3}, call_id="c2"),
        text("依据[1]。"),
    )
    loop = make_loop(llm, registry)
    result = await loop.run("问题")

    assert result.stop_reason is StopReason.ANSWERED
    assert len(result.citations) == 1
    assert result.citations[0].index == 1
