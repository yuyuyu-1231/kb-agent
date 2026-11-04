r"""第一次端到端验证：harness 内核 + 真实 DeepSeek。

检索工具这里是假数据（返回固定的两段制度），第 2 周换成真实 Qdrant 检索。
但除了"数据从哪来"，其余全部走真实链路：

    AgentLoop -> ContextManager 裁剪 -> Guard 检查 -> DeepSeekLLMClient
              -> ToolRegistry 执行 -> ToolResult 回喂 -> 收敛 -> Tracer 落盘

成本：约 2-3 次 LLM 调用，几分钱。
结果写入 E:\DevCaches\kb_e2e_report.txt
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings          # noqa: E402
from app.infra.llm import OpenAILLMClient         # noqa: E402
from harness.context import ContextManager        # noqa: E402
from harness.loop import AgentLoop                # noqa: E402
from harness.tools import ToolRegistry            # noqa: E402
from harness.trace import replay                  # noqa: E402

OUT = r"E:\DevCaches\kb_e2e_report.txt"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


FAKE_CHUNKS = [
    {
        "text": "员工累计工作满 1 年不满 10 年的，年休假 5 天；满 10 年不满 20 年的，年休假 10 天；满 20 年的，年休假 15 天。",
        "doc_name": "员工手册.pdf",
        "page_no": 12,
        "section_title": "第三章 假期制度",
        "score": 0.83,
    },
    {
        "text": "试用期包含在劳动合同期限内。试用期员工与正式员工享有同等休假权利，但需转正后方可申请年休假。",
        "doc_name": "员工手册.pdf",
        "page_no": 13,
        "section_title": "第三章 假期制度",
        "score": 0.79,
    },
]


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool
    async def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
        """在企业知识库中检索与问题相关的文档片段。

        Args:
            query: 自然语言检索词
            top_k: 返回条数
        """
        return FAKE_CHUNKS[:top_k]

    return registry


async def main() -> None:
    settings = get_settings()

    say("=" * 70)
    say("端到端验证：harness 内核 + 真实 DeepSeek")
    say("=" * 70)
    say(f"  model      = {settings.llm_model_id}")
    say(f"  base_url   = {settings.llm_base_url}")
    say(f"  max_steps  = {settings.max_steps}")
    say(f"  token_budget = {settings.token_budget}")
    say(f"  trace_dir  = {settings.trace_dir}")

    llm = OpenAILLMClient.from_settings(settings)
    registry = build_registry()

    # 上下文配置走 ContextManager，不经过 AgentLoop。
    # 内核只接受一个已配好的 context 实例 —— 这是依赖注入，
    # loop 不需要知道 context 到底有多少个可调参数。
    context = ContextManager(
        budget=settings.token_budget,
        tool_result_max_chars=settings.tool_result_max_chars,
        cn_ratio=settings.token_estimate_cn,
        other_ratio=settings.token_estimate_other,
    )

    loop = AgentLoop(
        llm=llm,
        registry=registry,
        context=context,
        trace_dir=settings.trace_dir,
        artifact_dir=settings.artifact_dir,
        max_steps=settings.max_steps,
        token_budget=settings.token_budget,
    )

    question = "试用期员工能休年假吗？"
    say("")
    say("-" * 70)
    say(f"提问：{question}")
    say("-" * 70)

    result = await loop.run(question)

    say("")
    say("=" * 70)
    say("结果")
    say("=" * 70)
    say(f"  stop_reason = {result.stop_reason.value}")
    say(f"  steps       = {result.steps}")
    say(f"  tokens      = {result.tokens}")
    say(f"  latency_ms  = {result.latency_ms}")
    say(f"  trace_id    = {result.trace_id}")
    say("")
    say("  ---- 回答 ----")
    for line in result.answer.splitlines():
        say(f"  {line}")

    say("")
    say(f"  ---- 引用 {len(result.citations)} 条 ----")
    for c in result.citations:
        say(f"  [{c.index}] {c.doc_name} 第 {c.page_no} 页 | {c.section_title}")
        say(f"       {c.snippet[:60]}...")
        say(f"       score={c.score}")

    # ---- 回放 trace，验证可观测性链路 ----
    say("")
    say("=" * 70)
    say("Trace 回放")
    say("=" * 70)
    records = replay(settings.trace_dir, result.trace_id,
                     artifact_dir=settings.artifact_dir)
    say(f"  共 {len(records)} 条记录")
    for rec in records:
        kind = rec.get("kind")
        step = rec.get("step")
        if kind == "final":
            say(f"  [{step}] FINAL  stop={rec.get('stop_reason')} "
                f"tokens={rec.get('tokens')} total_ms={rec.get('total_latency_ms')}")
        else:
            tool = rec.get("tool") or "-"
            thought = str(rec.get("thought", ""))[:50]
            say(f"  [{step}] {tool:<18} ok={rec.get('ok')} "
                f"{rec.get('latency_ms')}ms tokens={rec.get('tokens')} | {thought}")

    await llm.aclose()


try:
    asyncio.run(main())
except Exception as exc:  # noqa: BLE001
    import traceback
    say("")
    say("[FAIL] 运行异常：")
    for line in traceback.format_exc().splitlines():
        say(f"  {line}")
finally:
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}")
