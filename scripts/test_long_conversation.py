r"""长对话测试：逼出上下文裁剪和工具返回截断。

## 为什么需要这个脚本

`docs/PROGRESS.md` 的「验证缺口」里记着三条：

1. 上下文裁剪四级 —— trace 里 `[context]` **0 次**
2. 工具返回截断 —— trace 里 `[truncated]` **0 次**
3. 多轮会话记忆 —— 已用 curl 验过（11/8）

前两条的共同原因是**数据规模不够**：50 条评估题平均 6716 token，
离 30000 的预算很远；工具返回 5 条 chunk 约 800 字符，离 2000 的上限也远。

本脚本用两种手段把它们逼出来：

- **多轮 × 高召回**：12 轮对话，其中几轮让 top_k 拉到 20，
  单轮工具返回必然超过 2000 字符 → 触发截断
- **小预算模拟长对话**：真实 30000 预算下 12 轮也打不到，所以额外跑一组
  `ContextManager(budget=6000)`，用同一份裁剪代码验证逻辑正确性

## 一个必须分开的两个 budget

`AgentLoop` 的 `token_budget` 同时传给了两个地方：

    ContextManager(budget=...)   ← 单次请求的上下文**大小**上限（裁剪目标）
    Guard(token_budget=...)      ← 一次 run 的**累计消耗**上限（熔断阈值）

语义完全不同。如果为了触发裁剪把 `token_budget` 调到 6000，
第 2 轮累计消耗就超了，会先触发 `budget_exhausted` 熔断，
根本跑不到第 12 轮。

所以脚本里显式构造 `ContextManager(budget=6000)` 单独传，
同时把 `token_budget` 放到 60000 给 Guard 留足空间。

## 语料隔离

三份 PDF 用不同排版（单栏 / 双栏 / 带页眉页脚）生成，
灌进独立 collection `kb_longtest`，**主库 kb_chunks 的 14 个 chunk 不受影响**。

## 跑法

    python scripts/test_long_conversation.py
"""

import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings                     # noqa: E402
from app.infra.embedding import Embedder                     # noqa: E402
from app.infra.llm import OpenAILLMClient                    # noqa: E402
from app.infra.qdrant import QdrantStore                     # noqa: E402
from app.services.ingest import parse_pdf                    # noqa: E402
from app.services.retrieval import Retriever                 # noqa: E402
from app.services.ticket import TicketStore                  # noqa: E402
from app.tools.knowledge import register_knowledge_tools     # noqa: E402
from app.tools.ticket import register_ticket_tools           # noqa: E402
from harness.context import ContextManager                   # noqa: E402
from harness.loop import AgentLoop                           # noqa: E402
from harness.memory import SessionMemory                     # noqa: E402
from harness.tools import ToolRegistry                       # noqa: E402
from harness.trace import replay                             # noqa: E402

TEST_DIR = ROOT / "data" / "corpus_layout"
COLLECTION = "kb_longtest"
OUT = ROOT / "docs" / "LONG_CONVERSATION.md"
SRC_PDF = ROOT / "data" / "corpus" / "员工手册.pdf"

# 12 轮问题。设计意图写在注释里：
TURNS: list[tuple[str, str]] = [
    ("试用期员工能休年假吗？", "建立话题：年假"),
    ("那病假呢？", "指代消解：那 = 年假"),
    ("请病假需要提供什么材料？", "同话题深入"),
    ("这两个假期的工资分别怎么算？", "跨轮对比"),
    ("工作日加班工资怎么算？", "切话题"),
    ("那周六加班呢？", "指代消解：那 = 加班"),
    ("迟到 20 分钟怎么处理？", "切话题"),
    ("迟到超过 2 小时呢？", "同话题边界"),
    ("报销有什么时限要求？", "切话题"),
    ("去北京出差住宿标准是多少？", "定值问题"),
    ("如果有五线城市的标准吗？", "诱导检索：库里没有五线"),
    ("把前面聊过的假期规则总结一下", "记忆终验：裁剪后还记不记得第 1 轮"),
]


def load_text() -> str:
    """从现有手册提取纯文本，作为三份变体的内容源。"""
    doc = fitz.open(str(SRC_PDF))
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    text = "\n".join(pages).strip()
    if len(text) < 200:
        raise RuntimeError("源 PDF 提取到的文本太少，无法生成变体")
    return text


def _write_flat_pages(doc, text: str, per_page: int = 1000, fontsize: float = 9) -> None:
    """按估算字符数分页写入。

    为什么必须手动分页：`insert_textbox` 放不下时返回负数，**超出的部分
    直接丢弃且不报错**。不分页会得到一个内容残缺的 PDF，然后
    ingest 那边报“chunk 太少”之类看不出根因的错。
    """
    for i in range(0, len(text), per_page):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(
            fitz.Rect(50, 50, 545, 790),
            text[i:i + per_page],
            fontsize=fontsize,
            fontname="china-s",
        )


def make_pdf(text: str, style: str, out_path: Path) -> None:
    """按指定排版生成 PDF。

    三种排版都用扁平文本（去掉换行），这样字符数好估算，也不会因为
    段落结构导致某页放不下。测的是“排版差异能不能被 ingest 正确处理”。
    """
    flat = text.replace("\n", "")
    doc = fitz.open()
    try:
        if style == "two_column":
            # 双栏：每页左右各放一半。这是最容易让提取顺序出错的排版。
            for start in range(0, len(flat), 1200):
                seg = flat[start:start + 1200]
                cut = len(seg) // 2
                page = doc.new_page(width=595, height=842)
                page.insert_textbox(
                    fitz.Rect(45, 50, 290, 790), seg[:cut],
                    fontsize=8, fontname="china-s",
                )
                page.insert_textbox(
                    fitz.Rect(305, 50, 550, 790), seg[cut:],
                    fontsize=8, fontname="china-s",
                )

        elif style == "header_footer":
            # 带页眉页脚：测 ingest 的页眉页脚剔除。
            # 页眉在顶部 10% 内、内容每页重复 —— 正好命中检测的两个条件。
            for i in range(0, len(flat), 1000):
                page = doc.new_page(width=595, height=842)
                page.insert_textbox(
                    fitz.Rect(50, 30, 545, 55),
                    "示例科技有限公司 · 内部资料",
                    fontsize=8, fontname="china-s",
                )
                page.insert_textbox(
                    fitz.Rect(50, 60, 545, 780), flat[i:i + 1000],
                    fontsize=9, fontname="china-s",
                )
                page.insert_textbox(
                    fitz.Rect(50, 790, 545, 815),
                    f"第 {i // 1000 + 1} 页 · 仅供内部使用",
                    fontsize=8, fontname="china-s",
                )

        elif style == "flat_long":
            # 无结构长文：内容重复 3 遍。
            #
            # **这是专门用来逼截断的**。现有手册的 chunk 平均只有 100 字
            # （按章节切），5 条加起来才 800 字符，离 2000 的阈值太远。
            # 去掉换行后每块都能填满 chunk_size=400，5 条就超 2000。
            _write_flat_pages(doc, (flat + " ") * 10, per_page=1000)

        else:
            raise ValueError(f"未知排版: {style}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
    finally:
        doc.close()


async def load_layouts(settings, embedder: Embedder) -> tuple[QdrantStore, list[dict]]:
    """生成三份不同排版的 PDF 并灌进独立 collection。"""
    base_text = load_text()
    store = QdrantStore(
        url=settings.qdrant_url, collection=COLLECTION, dim=settings.embed_dim,
    )
    await store.ensure_collection()

    layouts = [
        ("two_column", "双栏"),
        ("header_footer", "带页眉页脚"),
        ("flat_long", "无结构长文（逼截断用）"),
    ]

    stats: list[dict] = []
    for style, label in layouts:
        path = TEST_DIR / f"制度汇编-{label}.pdf"
        make_pdf(base_text, style, path)

        chunks, result = parse_pdf(path, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap)
        vectors = await embedder.embed_passages([c["text"] for c in chunks])
        await store.upsert_chunks(
            doc_id=path.stem,
            doc_name=path.name,
            chunks=chunks,
            vectors=vectors,
        )
        stats.append({
            "label": label,
            "pages": result.pages,
            "blocks": result.blocks,
            "dropped_margin": result.dropped_margin_blocks,
            "chunks": result.chunks,
        })
    return store, stats


def scan_trace(trace_dir: str, trace_id: str) -> dict:
    """从 trace 里提取本轮是否发生裁剪 / 截断。"""
    try:
        records = replay(trace_dir, trace_id)
    except Exception:  # noqa: BLE001
        return {"trimmed": False, "truncated": False, "tools": []}

    trimmed = False
    truncated = False
    tools: list[str] = []
    for rec in records:
        thought = str(rec.get("thought") or "")
        tool = str(rec.get("tool") or "")
        if tool and not tool.startswith("__"):
            tools.append(tool)

        # 裁剪动作写在 thought 里，格式是 "[context] 截断 N 条超长工具结果；…"
        # 所以判断是否发生了「截断」应该看这段文字，而不是看 observation ——
        # trace 里的 observation 本身就是被截断存储的（只保留前 120 字符），
        # 里面的 [truncated] 标记根本不会出现在 trace 里。
        if "[context]" in thought:
            trimmed = True
            if "截断" in thought:
                truncated = True
    return {"trimmed": trimmed, "truncated": truncated, "tools": tools}


async def run_session(
    loop: AgentLoop,
    memory: SessionMemory,
    session_id: str,
    label: str,
    trace_dir: str,
) -> list[dict]:
    rows: list[dict] = []
    for idx, (question, intent) in enumerate(TURNS, start=1):
        started = time.perf_counter()
        result = await loop.run(question, session_id=session_id)
        elapsed = int((time.perf_counter() - started) * 1000)

        scan = scan_trace(trace_dir, result.trace_id)
        history = memory.load(session_id)
        rows.append({
            "turn": idx,
            "question": question,
            "intent": intent,
            "stop_reason": result.stop_reason.value,
            "steps": result.steps,
            "tokens": result.tokens,
            "latency_ms": elapsed,
            "trimmed": scan["trimmed"],
            "truncated": scan["truncated"],
            "history_messages": len(history),
            "answer": result.answer,
        })
        print(
            f"  [{label} {idx:>2}/12] {result.stop_reason.value:<15} "
            f"tok={result.tokens:<6} hist={len(history):<3} "
            f"trim={'Y' if scan['trimmed'] else '.'} "
            f"trunc={'Y' if scan['truncated'] else '.'} "
            f"{elapsed}ms | {question[:18]}"
        )
    return rows


async def main() -> None:
    settings = get_settings()
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    say("# 长对话测试报告")
    say("")
    say("目标：把「上下文裁剪」和「工具返回截断」从“写了但没触发过”变成“跑过且有数据”。")
    say("")

    embedder = Embedder.from_settings(settings)
    say("## 一、三份不同排版的语料")
    say("")
    store, layout_stats = await load_layouts(settings, embedder)
    await store.ensure_collection()

    say("| 排版 | 页数 | 文本块 | 剔页眉页脚 | chunk 数 |")
    say("|---|---|---|---|---|")
    for s in layout_stats:
        say(f"| {s['label']} | {s['pages']} | {s['blocks']} | "
            f"{s['dropped_margin']} | {s['chunks']} |")
    say("")
    say(f"> 语料灌在独立 collection `{COLLECTION}`，主库 `kb_chunks` 不受影响。")
    say("")

    retriever = Retriever.from_infra(embedder, store)
    tickets = TicketStore(ROOT / "data" / "longtest_tickets.db")
    memory = SessionMemory(ROOT / "data" / "longtest_sessions.db")
    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)
    register_ticket_tools(registry, tickets)

    llm = OpenAILLMClient.from_settings(settings)

    def make_loop(context_budget: int, guard_budget: int) -> AgentLoop:
        return AgentLoop(
            llm=llm,
            registry=registry,
            memory=memory,
            # 这两个 budget 语义不同，必须分开传：
            #   context  → 单次请求的上下文大小上限（裁剪目标）
            #   guard    → 一次 run 的累计消耗上限（熔断阈值）
            context=ContextManager(budget=context_budget),
            token_budget=guard_budget,
            trace_dir=settings.trace_dir,
            artifact_dir=settings.artifact_dir,
            max_steps=settings.max_steps,
        )

    # ---- 阶段 A：真实预算（30000）----
    say("## 二、阶段 A：真实预算 30000，跑 12 轮")
    say("")
    loop_a = make_loop(context_budget=settings.token_budget, guard_budget=60000)
    rows_a = await run_session(
        loop_a, memory, f"longA-{uuid4().hex[:8]}", "A", str(settings.trace_dir),
    )
    say("")

    # ---- 阶段 B：小预算模拟长对话（6000）----
    say("## 三、阶段 B：裁剪预算 6000，跑 12 轮")
    say("")
    say("> 真实 30000 下 12 轮打不到上限，所以用小预算模拟“更长的对话”。")
    say("> 裁剪代码是同一份，验证的是「裁剪发生时不出错、不丢关键信息」。")
    say("")
    loop_b = make_loop(context_budget=6000, guard_budget=60000)
    rows_b = await run_session(
        loop_b, memory, f"longB-{uuid4().hex[:8]}", "B", str(settings.trace_dir),
    )
    say("")

    # ---- 汇总 ----
    say("## 四、结果")
    say("")
    say("| 阶段 | 裁剪预算 | 峰值 token | 首次裁剪轮次 | 截断出现 | 末轮记忆 |")
    say("|---|---|---|---|---|---|")

    for label, rows, budget in (("A", rows_a, settings.token_budget), ("B", rows_b, 6000)):
        peak = max(r["tokens"] for r in rows)
        first_trim = next((r["turn"] for r in rows if r["trimmed"]), None)
        trunc_count = sum(1 for r in rows if r["truncated"])
        last = rows[-1]["answer"]
        # 末轮问的是“总结假期规则”，能提到年假天数就算还记得第 1 轮
        remembered = "5 天" in last or "五天" in last
        say(
            f"| {label} | {budget} | {peak} | "
            f"{first_trim if first_trim else '未触发'} | "
            f"{trunc_count} 轮 | {'记得' if remembered else '不记得'} |"
        )
    say("")

    say("## 五、逐轮明细")
    say("")
    for label, rows in (("A（真实预算 30000）", rows_a), ("B（裁剪预算 6000）", rows_b)):
        say(f"### 阶段 {label}")
        say("")
        say("| 轮 | 意图 | 问题 | token | 历史条数 | 裁剪 | 截断 |")
        say("|---|---|---|---|---|---|---|")
        for r in rows:
            say(
                f"| {r['turn']} | {r['intent']} | {r['question'][:22]} | "
                f"{r['tokens']} | {r['history_messages']} | "
                f"{'是' if r['trimmed'] else '—'} | "
                f"{'是' if r['truncated'] else '—'} |"
            )
        say("")

    say("## 六、结论")
    say("")
    trimmed_any = any(r["trimmed"] for r in rows_a + rows_b)
    truncated_any = any(r["truncated"] for r in rows_a + rows_b)

    say(f"- 上下文裁剪：{'**已触发**' if trimmed_any else '**仍未触发**'}")
    say(f"- 工具返回截断：{'**已触发**' if truncated_any else '**仍未触发**'}")
    say("")
    if not trimmed_any:
        say("裁剪仍未触发，说明连 6000 预算都装得下 12 轮内容。")
        say("要验证裁剪逻辑，需要更大的历史或更小的预算，见下一步建议。")
    if not truncated_any:
        say("截断仍未触发，说明单轮检索结果的字符数没到 2000。")
        say("可尝试把 top_k 提到 20 以上，或换成更长的 chunk。")
    say("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("")
    print(f"-> {OUT}")

    await llm.aclose()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
