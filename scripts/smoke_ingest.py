r"""真实摄入验证：PDF -> 解析 -> 清洗 -> 分块 -> 嵌入 -> Qdrant 入库 -> 检索。

结果写入 E:\DevCaches\kb_ingest_report.txt
"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings          # noqa: E402
from app.infra.embedding import Embedder          # noqa: E402
from app.infra.qdrant import QdrantStore          # noqa: E402
from app.services.ingest import ingest_pdf, parse_pdf  # noqa: E402

OUT = r"E:\DevCaches\kb_ingest_report.txt"
PDF = ROOT / "data" / "corpus" / "员工手册.pdf"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


async def main() -> None:
    settings = get_settings()

    say("=" * 72)
    say("真实摄入验证")
    say("=" * 72)
    say(f"  pdf          = {PDF}")
    say(f"  chunk_size   = {settings.chunk_size}")
    say(f"  overlap      = {settings.chunk_overlap}")

    if not PDF.exists():
        say(f"  [FAIL] 文件不存在")
        return

    # ---- 1. 只做解析，先看清洗和分块质量 ----
    say("")
    say("-" * 72)
    say("1. 解析与清洗（未入库）")
    say("-" * 72)

    chunks, parsed = parse_pdf(
        PDF,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
    )
    say(f"  页数            = {parsed.pages}")
    say(f"  保留文本块      = {parsed.blocks}")
    say(f"  剔除页眉页脚    = {parsed.dropped_margin_blocks} 块")
    say(f"  切出 chunk      = {parsed.chunks}")
    say(f"  识别到的章节    = {len(parsed.sections)}")
    for s in parsed.sections:
        say(f"      - {s}")

    say("")
    say("  ---- 各 chunk 概览 ----")
    for c in chunks:
        text = c["text"].replace("\n", " ")
        say(f"  [{c['chunk_index']:>2}] p.{c['page_no']} "
            f"§{c['section_title'][:14]:<14} "
            f"len={len(text):>3} | {text[:52]}")

    # ---- 2. 入库 ----
    say("")
    say("-" * 72)
    say("2. 嵌入并入库")
    say("-" * 72)

    embedder = Embedder.from_settings(settings)
    store = QdrantStore.from_settings(settings)
    await store.ensure_collection()

    t0 = time.perf_counter()
    result = await ingest_pdf(
        PDF,
        embedder=embedder,
        store=store,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    say(f"  doc_id          = {result.doc_id}")
    say(f"  doc_name        = {result.doc_name}")
    say(f"  写入 chunk      = {result.chunks}")
    say(f"  耗时（含首次加载模型） = {elapsed} ms")
    say(f"  库内总 points   = {await store.count()}")

    # 幂等性：再入库一次，数量不应该翻倍
    await ingest_pdf(
        PDF, embedder=embedder, store=store,
        chunk_size=settings.chunk_size, overlap=settings.chunk_overlap,
    )
    say(f"  重复入库后 points = {await store.count()}（应保持不变）")

    # ---- 3. 检索 ----
    say("")
    say("-" * 72)
    say("3. 用真实问题检索")
    say("-" * 72)

    questions = [
        "试用期员工能休年假吗？",
        "差旅住宿费标准是多少？",
        "迟到超过半小时怎么算？",
        "加班费怎么计算？",
        "公司年终奖怎么发？",       # 知识库里没有，看分数是否明显偏低
    ]
    for q in questions:
        qv = await embedder.embed_query(q)
        hits = await store.search(qv, top_k=3)
        say("")
        say(f"  Q: {q}")
        if not hits:
            say("    (无结果)")
            continue
        for rank, h in enumerate(hits, start=1):
            score = h["score"] or 0.0
            say(f"    #{rank} {score:.4f} p.{h['page_no']} [{h['section_title'][:16]}]")
            say(f"        {h['text'][:70]}")

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
