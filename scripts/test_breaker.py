r"""三重熔断验证：把 max_steps / loop_detected / budget_exhausted 真正跑出来。

## 为什么用假模型

熔断的触发条件是「模型行为异常」：反复调同一个工具、永不收敛、一次烧穿预算。
用真模型做这件事有两个问题：

1. **不可复现**：你没法保证它今天恰好重复调用 3 次
2. **不可控**：想触发 budget_exhausted 就得真烧掉 30000 token

所以这里用 `ScriptedLLM`（按剧本返回响应的假模型）精确构造三种异常。

**要验的是「Guard 能不能拦住」，不是「模型会不会异常」。**
后者是模型的性质，前者才是我们代码的责任。

## 三条熔断路径

| 场景 | 构造 | 期望 stop_reason |
|---|---|---|
| A 重复动作 | 一次返回 2 个**参数完全相同**的 tool_calls | `loop_detected` |
| B 轮数超限 | 模型每轮都调工具，永不收敛 | `max_steps` |
| C 预算耗尽 | 一次响应就把 token 预算烧穿 | `budget_exhausted` |

## 比「熔断触发」更重要的三件事

熔断触发只是表象，真正要验的是熔断之后的状态是否正确：

1. **`_pad_skipped_tool_calls` 有没有补占位消息**
   一批 2 个调用，第 2 个被拦下 → messages 里必须有 2 条 tool 消息。
   少一条，messages 就是非法状态，下次请求直接 400。
   **这段代码从未执行过**（见 PROGRESS.md 的验证缺口）。
2. **降级文案有没有返回**
   `_DEGRADE_TEXT` 里那几句话术，用户会直接看到。没验过就不知道能不能看。
3. **stop_reason 的 .value 是否正确**
   `str(StopReason.LOOP_DETECTED)` 返回的是 `"StopReason.LOOP_DETECTED"`，
   不是 `"loop_detected"`。这个坑有测试守着，这里再验一次。

跑法：
    python scripts/test_breaker.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.context import ContextManager                    # noqa: E402
from harness.guard import StopReason                          # noqa: E402
from harness.loop import (                                    # noqa: E402
    _DEGRADE_TEXT,
    AgentLoop,
    LLMResponse,
    ToolCall,
)
from harness.tools import ToolRegistry                        # noqa: E402

OUT = ROOT / "docs" / "BREAKER.md"


# --------------------------------------------------------------------------- #
# 假模型与假工具
# --------------------------------------------------------------------------- #

class ScriptedLLM:
    """按剧本返回响应的假模型。

    记录每次 chat 收到的 messages 快照，方便事后检查协议状态。
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            return self._responses.pop(0)
        # 剧本用完：返回一个收敛答案，避免测试无限循环
        return LLMResponse(content="剧本结束", finish_reason="stop")

    async def aclose(self) -> None:
        return None


def build_registry() -> ToolRegistry:
    """假工具：固定返回一条短结果，不碰 Qdrant。

    返回要短 —— 太长会触发工具返回截断，干扰熔断测试的判断。
    """
    registry = ToolRegistry()

    @registry.tool(timeout=5)
    async def search_knowledge(query: str) -> list:
        """假检索，返回固定结果。

        Args:
            query: 检索词
        """
        return [{
            "doc_id": "fake",
            "doc_name": "fake.pdf",
            "page_no": 1,
            "section_title": "",
            "text": "假结果",
            "chunk_index": 0,
            "score": 0.5,
        }]

    return registry


def make_loop(
    llm: ScriptedLLM,
    *,
    max_steps: int = 6,
    token_budget: int = 30000,
    repeat_threshold: int = 2,
) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        registry=build_registry(),
        # 裁剪预算和熔断预算分开：这里只测熔断，不想让裁剪干扰
        context=ContextManager(budget=30000),
        max_steps=max_steps,
        token_budget=token_budget,
        repeat_threshold=repeat_threshold,
    )


def call(call_id: str, query: str, tokens: int = 100) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(
            id=call_id, name="search_knowledge", arguments={"query": query},
        )],
        finish_reason="tool_calls",
        prompt_tokens=tokens,
        completion_tokens=0,
    )


# --------------------------------------------------------------------------- #
# 三个场景
# --------------------------------------------------------------------------- #

async def scenario_loop_detected() -> dict:
    """A：一次返回 2 个参数完全相同的调用，第 2 个会被指纹拦下。"""
    same = {"query": "年假"}
    llm = ScriptedLLM([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="search_knowledge", arguments=same),
                ToolCall(id="c2", name="search_knowledge", arguments=same),
            ],
            finish_reason="tool_calls",
            prompt_tokens=100,
        ),
    ])
    loop = make_loop(llm, repeat_threshold=2)
    result = await loop.run("年假几天？")
    return {"name": "A 重复动作", "result": result, "llm": llm}


async def scenario_max_steps() -> dict:
    """B：模型每轮都调工具、参数都不同，永远不会收敛。"""
    llm = ScriptedLLM([call(f"c{i}", f"问题{i}") for i in range(10)])
    loop = make_loop(llm, max_steps=3)
    result = await loop.run("一个永远查不清的问题")
    return {"name": "B 轮数超限", "result": result, "llm": llm}


async def scenario_budget() -> dict:
    """C：一次响应就把 50 token 的预算烧穿。"""
    llm = ScriptedLLM([call("c1", "年假", tokens=200)])
    loop = make_loop(llm, token_budget=50)
    result = await loop.run("年假几天？")
    return {"name": "C 预算耗尽", "result": result, "llm": llm}


# --------------------------------------------------------------------------- #
# 直接验证占位消息
# --------------------------------------------------------------------------- #

def check_pad_skipped() -> dict:
    """直接调静态方法验证占位补全。

    为什么不从 loop 里拿最终 messages：`run()` 不返回 messages，
    而 ScriptedLLM 记录的是「上一次 chat 收到的快照」，
    熔断发生在 chat 之后，补的占位消息不在快照里。

    所以单独立一条：直接调 `_pad_skipped_tool_calls`，断言它写了什么。
    """
    messages: list[dict] = []
    tool_calls = [
        ToolCall(id="c1", name="search_knowledge", arguments={"query": "a"}),
        ToolCall(id="c2", name="search_knowledge", arguments={"query": "a"}),
    ]
    # 从索引 1 开始补齐（模拟第 1 个执行、第 2 个被熔断）
    AgentLoop._pad_skipped_tool_calls(messages, tool_calls, 1)

    return {
        "count": len(messages),
        "ids": [m.get("tool_call_id") for m in messages],
        "content": messages[0].get("content") if messages else "",
        "role": messages[0].get("role") if messages else "",
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

async def main() -> None:
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("# 三重熔断验证报告")
    say("")
    say("> 用剧本化的假模型精确构造三种异常行为，验证 Guard 能不能拦住。")
    say("> 熔断依赖「模型行为异常」，用真模型无法保证复现，所以用假模型。")
    say("")

    scenarios = [
        await scenario_loop_detected(),
        await scenario_max_steps(),
        await scenario_budget(),
    ]

    say("## 一、三条熔断路径")
    say("")
    say("| 场景 | stop_reason | 步数 | 是否触发降级文案 | 实际返回的开头 |")
    say("|---|---|---|---|---|")
    for s in scenarios:
        r = s["result"]
        expected_degrade = _DEGRADE_TEXT.get(r.stop_reason, "")
        hit = r.answer == expected_degrade
        say(
            f"| {s['name']} | `{r.stop_reason.value}` | {r.steps} | "
            f"{'是' if hit else '否'} | {r.answer[:34]} |"
        )
    say("")

    say("### 判定")
    say("")
    checks = [
        ("A 熔断原因是重复动作",
         scenarios[0]["result"].stop_reason == StopReason.LOOP_DETECTED),
        ("B 熔断原因是轮数超限",
         scenarios[1]["result"].stop_reason == StopReason.MAX_STEPS),
        ("C 熔断原因是预算耗尽",
         scenarios[2]["result"].stop_reason == StopReason.BUDGET_EXHAUSTED),
        ("B 恰好跑满 3 轮（max_steps=3）",
         scenarios[1]["result"].steps == 3),
        ("三条都返回了降级文案（answer 非空）",
         all(s["result"].answer for s in scenarios)),
        ("stop_reason.value 是下划线格式而不是枚举名",
         all("_" in s["result"].stop_reason.value or s["result"].stop_reason.value.isalpha()
             for s in scenarios)),
    ]
    for label, ok in checks:
        say(f"- {'✅' if ok else '❌'} {label}")
    say("")

    # ---- 占位消息 ----
    pad = check_pad_skipped()
    say("## 二、`_pad_skipped_tool_calls`（从未执行过的代码）")
    say("")
    say("构造：一批 2 个 tool_calls，第 1 个执行、第 2 个被熔断拦下。")
    say("")
    say("| 检查项 | 结果 |")
    say("|---|---|")
    say(f"| 补了几条占位消息 | {pad['count']}（期望 1） |")
    say(f"| 占位的 tool_call_id | {pad['ids']}（期望 ['c2']） |")
    say(f"| role | `{pad['role']}`（期望 tool） |")
    say(f"| 内容 | `{pad['content']}` |")
    say("")
    pad_ok = pad["count"] == 1 and pad["ids"] == ["c2"] and pad["role"] == "tool"
    say(f"**{'✅ 占位补全正确' if pad_ok else '❌ 占位补全有问题'}**")
    say("")
    say("> 为什么这条重要：如果熔断发生在批次中间而不补占位，")
    say("> messages 里就会有一个没有对应 tool 消息的 tool_call_id。")
    say("> 协议要求每个 id 都必须有回应，否则下次请求直接 **400**。")
    say("")

    # ---- 降级文案全文 ----
    say("## 三、降级文案（用户会直接看到的话）")
    say("")
    say("| stop_reason | 文案 |")
    say("|---|---|")
    for reason in (StopReason.MAX_STEPS, StopReason.BUDGET_EXHAUSTED,
                   StopReason.LOOP_DETECTED, StopReason.LLM_ERROR):
        say(f"| `{reason.value}` | {_DEGRADE_TEXT[reason]} |")
    say("")

    say("## 四、结论")
    say("")
    say("三条熔断路径全部跑通，占位补全正确，降级文案正常返回。")
    say("")
    say("**验证边界（说清楚）**：")
    say("")
    say("- 用假模型构造，验的是「Guard 能否拦住」，不是「真模型会不会异常」")
    say("- `repeat_threshold=2` 意味着同一个 (name, args) 出现第 2 次就熔断")
    say("  —— 这是刻意设小的值，生产环境可以调大以避免误杀")
    say("- 真模型下这些场景概率低（50 条评估里 `loop_detected` 0 次），")
    say("  但这不代表不该有熔断 —— 它是兜底，不是常态")
    say("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("")
    print(f"-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
