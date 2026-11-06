r"""端到端验证：知识库 + 工单，四个工具全部接入。

验证三个场景：
1. 库内问题 -> 直接回答，不建单
2. 库外问题 -> 自主建单，返回工单号
3. 追问工单进度 -> 调用 check_ticket_status

结果写入 E:\DevCaches\kb_e2e_ticket.txt
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
from app.services.ticket import TicketStore       # noqa: E402
from app.tools.knowledge import register_knowledge_tools  # noqa: E402
from app.tools.ticket import register_ticket_tools        # noqa: E402
from harness.loop import AgentLoop                # noqa: E402
from harness.tools import ToolRegistry            # noqa: E402
from harness.trace import replay                  # noqa: E402

OUT = r"E:\DevCaches\kb_e2e_ticket.txt"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


SYSTEM_PROMPT = """你是企业知识库客服助手。

工作流程：
1. 先调用 search_knowledge 检索知识库。
2. 找到依据就回答，每条结论用 [n] 标注来源，n 是检索结果里的 index 字段。
3. 检索 2 次仍找不到依据，或者问题涉及必须人工核实的记录（账号状态、订单金额、
   费用结算、系统故障），必须调用 create_ticket 建单，不要只给一句"建议咨询 HR"。
4. 建单后，在回答里明确告知工单号和处理优先级。
5. 用户询问已有工单进度时，调用 check_ticket_status 查询。

其他要求：
- 检索不到依据时不要编造。
- 检索片段末尾有 [truncated] 标记时，用 read_document_page 读原文。
- 回答简洁，先结论后依据。"""


def tool_trace(trace_dir: str, trace_id: str) -> list[str]:
    records = replay(trace_dir, trace_id)
    return [
        f"step{r.get('step')}: {r.get('tool')} {r.get('args')}"
        for r in records
        if r.get("tool") and not str(r.get("tool")).startswith("__")
    ]


async def main() -> None:
    settings = get_settings()

    say("=" * 76)
    say("端到端验证：知识库 + 工单（四个工具）")
    say("=" * 76)

    embedder = Embedder.from_settings(settings)
    vector_store = QdrantStore.from_settings(settings)
    await vector_store.ensure_collection()
    retriever = Retriever.from_infra(embedder, vector_store)
    ticket_store = TicketStore(settings.sqlite_path)

    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)
    register_ticket_tools(registry, ticket_store)

    say(f"  注册工具   = {registry.names()}")
    say(f"  库内 chunk = {await vector_store.count()}")
    say(f"  已有工单   = {ticket_store.stats()}")
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

    # ---- 场景 1：库内问题，应该直接回答 ----
    say("=" * 76)
    say("场景 1｜库内问题：试用期员工能休年假吗？")
    say("预期：直接回答，不建单")
    say("-" * 76)

    r1 = await loop.run("试用期员工能休年假吗？")
    say(f"  stop_reason = {r1.stop_reason.value}")
    say(f"  steps = {r1.steps}  tokens = {r1.tokens}  latency = {r1.latency_ms}ms")
    say(f"  ticket_id   = {r1.ticket_id}")
    say("")
    for line in r1.answer.splitlines():
        say(f"  {line}")
    if r1.citations:
        say("")
        for c in r1.citations:
            say(f"  [{c.index}] p.{c.page_no} {c.section_title} score={c.score}")
    say("")
    say("  工具轨迹：")
    for t in tool_trace(settings.trace_dir, r1.trace_id):
        say(f"    {t}")
    say("")

    # ---- 场景 2：库外问题，应该建单 ----
    say("=" * 76)
    say("场景 2｜库外问题：公司年终奖怎么发？")
    say("预期：检索无果后自主建单，返回工单号")
    say("-" * 76)

    r2 = await loop.run("公司年终奖怎么发？我的工号是 A1024。")
    say(f"  stop_reason = {r2.stop_reason.value}")
    say(f"  steps = {r2.steps}  tokens = {r2.tokens}  latency = {r2.latency_ms}ms")
    say(f"  ticket_id   = {r2.ticket_id}")
    say("")
    for line in r2.answer.splitlines():
        say(f"  {line}")
    say("")
    say("  工具轨迹：")
    for t in tool_trace(settings.trace_dir, r2.trace_id):
        say(f"    {t}")
    say("")

    # ---- 场景 3：追问工单进度 ----
    ticket_id = r2.ticket_id
    if ticket_id:
        say("=" * 76)
        say(f"场景 3｜追问工单进度：{ticket_id} 处理得怎么样了？")
        say("预期：调用 check_ticket_status")
        say("-" * 76)

        r3 = await loop.run(f"我的工单 {ticket_id} 处理得怎么样了？")
        say(f"  stop_reason = {r3.stop_reason.value}")
        say(f"  steps = {r3.steps}  tokens = {r3.tokens}  latency = {r3.latency_ms}ms")
        say("")
        for line in r3.answer.splitlines():
            say(f"  {line}")
        say("")
        say("  工具轨迹：")
        for t in tool_trace(settings.trace_dir, r3.trace_id):
            say(f"    {t}")
    else:
        say("=" * 76)
        say("场景 3｜跳过：场景 2 没有建单")

    # ---- 落库核对 ----
    say("")
    say("=" * 76)
    say("工单落库核对")
    say("=" * 76)
    all_tickets = ticket_store.list(limit=10)
    say(f"  工单总数 = {len(all_tickets)}")
    for tk in all_tickets:
        say("")
        say(f"  单号     = {tk['id']}")
        say(f"  标题     = {tk['title']}")
        say(f"  类别     = {tk['category']}   优先级 = {tk['priority']}")
        say(f"  状态     = {tk['status']}")
        say(f"  摘要     = {str(tk['summary'])[:90]}")
        say(f"  证据     = {str(tk['evidence'])[:90]}")
        say(f"  创建时间 = {tk['created_at']}")

    await llm.aclose()
    await vector_store.close()


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
