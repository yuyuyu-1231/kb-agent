r"""端到端验证：真实 RAG。

链路：问题 -> AgentLoop -> DeepSeek 决定调工具 -> search_knowledge
      -> BGE 编码 query -> Qdrant 检索 -> ToolResult 回喂 -> 带页码答案

与 scripts/e2e_smoke.py 的区别：那个用的是硬编码的假 chunk，
这个用的是真实入库的 14 个 chunk。

结果写入 E:\DevCaches\kb_e2e_rag.txt
"""

import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings          # noqa: E402
from app.infra.embedding import Embedder          # noqa: E402
from app.infra.llm import OpenAILLMClient         # noqa: E402
from app.infra.qdrant import QdrantStore          # noqa: E402
from app.services.retrieval import Retriever      # noqa: E402
from app.tools.knowledge import register_knowledge_tools  # noqa: E402
from harness.loop import AgentLoop                # noqa: E402
from harness.tools import ToolRegistry            # noqa: E402
from harness.trace import replay                  # noqa: E402

OUT = r"E:\DevCaches\kb_e2e_rag.txt"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


SYSTEM_PROMPT = """你是企业知识库助手。回答必须基于检索到的文档内容。

规则：
1. 先用 search_knowledge 检索，再回答。
2. 检索结果里每条都有 index 字段。引用时用这个 index 作为编号，写成 [1] [2]。
   同一个 index 在整个对话里指向同一段内容，多次检索也不会变。
3. 检索不到依据时，明确说"未在知识库中找到相关规定"，不要编造。
4. 如果检索片段末尾有 [truncated] 标记，用 read_document_page 读原文。
5. 回答简洁，先给结论，再给依据。"""


QUESTIONS = [
    "试用期员工能休年假吗？",
    "差旅住宿费标准是多少？",
    "加班费怎么计算？",
    "公司年终奖怎么发？",        # 知识库里没有这一条，测拒答
]


async def main() -> None:
    settings = get_settings()

    say("=" * 74)
    say("真实 RAG 端到端验证")
    say("=" * 74)
    say(f"  model      = {settings.llm_model_id}")
    say(f"  collection = {settings.qdrant_collection}")

    embedder = Embedder.from_settings(settings)
    store = QdrantStore.from_settings(settings)
    await store.ensure_collection()

    retriever = Retriever.from_infra(embedder, store)
    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)

    say(f"  注册工具   = {registry.names()}")
    say(f"  库内 points = {await store.count()}")
    say("")

    llm = OpenAILLMClient.from_settings(settings)
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        system_prompt=SYSTEM_PROMPT,
        trace_dir=settings.trace_dir,
        artifact_dir=settings.artifact_dir,
        max_steps=settings.max_steps,
        token_budget=settings.token_budget,
    )

    for question in QUESTIONS:
        say("=" * 74)
        say(f"Q: {question}")
        say("-" * 74)

        start = time.perf_counter()
        result = await loop.run(question)
        elapsed = int((time.perf_counter() - start) * 1000)

        say(f"  stop_reason = {result.stop_reason.value}")
        say(f"  steps       = {result.steps}    tokens = {result.tokens}    "
            f"latency = {elapsed}ms")
        say("")
        for line in result.answer.splitlines():
            say(f"  {line}")

        if result.citations:
            say("")
            say(f"  ---- 引用 {len(result.citations)} 条 ----")
            for c in result.citations:
                say(f"  [{c.index}] {c.doc_name} p.{c.page_no} "
                    f"| {c.section_title} | score={c.score}")

        # 引用自检：答案里出现的每个 [n] 是否都有对应出处
        used = {int(m) for m in re.findall(r"\[(\d{1,3})\]", result.answer)}
        have = {c.index for c in result.citations}
        missing = sorted(used - have)
        if missing:
            say(f"  [WARN] 悬空引用（答案提到但无出处）：{missing}")
        elif used:
            say(f"  引用自检通过：{len(used)} 个编号全部有出处")

        # 从 trace 里看模型实际调了哪些工具、参数是什么
        records = replay(settings.trace_dir, result.trace_id)
        tool_steps = [
            (r.get("step"), r.get("tool"), r.get("args"), r.get("ok"))
            for r in records
            if r.get("tool") and not str(r.get("tool")).startswith("__")
        ]
        if tool_steps:
            say("")
            say(f"  ---- 工具调用轨迹 ----")
            for step, tool, args, ok in tool_steps:
                say(f"  step{step}: {tool}  args={args}  ok={ok}")
        say("")

    await llm.aclose()
    await store.close()


try:
    asyncio.run(main())
except Exception:  # noqa: BLE001
    import traceback
    say("")
    say("[FAIL] 未捕获异常：")
    for line in traceback.format_exc().splitlines():
        say(f"  {line}")
finally:
    Path(OUT).write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}")
