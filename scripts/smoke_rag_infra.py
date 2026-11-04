r"""验证真实 RAG 底层：fastembed 编码 + Qdrant 写入与检索。

这一步会真实下载 bge-small-zh-v1.5（约 90MB，落到 E:\DevCaches\fastembed），
并在本地 Qdrant 建 collection、写入几条测试数据、检索回来。

结果写入 E:\DevCaches\kb_rag_report.txt
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

OUT = r"E:\DevCaches\kb_rag_report.txt"
lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


TEST_CHUNKS = [
    {
        "text": "员工累计工作满 1 年不满 10 年的，年休假 5 天；满 10 年不满 20 年的，年休假 10 天；满 20 年的，年休假 15 天。",
        "page_no": 12, "section_title": "第三章 假期制度", "chunk_index": 0,
        "char_start": 3200, "char_end": 3300,
    },
    {
        "text": "试用期包含在劳动合同期限内。试用期员工与正式员工享有同等休假权利，但需转正后方可申请年休假。",
        "page_no": 13, "section_title": "第三章 假期制度", "chunk_index": 1,
        "char_start": 3400, "char_end": 3500,
    },
    {
        "text": "报销流程：员工在费用发生后 30 日内提交发票与审批单，逾期不予受理。差旅费按职级标准执行。",
        "page_no": 28, "section_title": "第五章 费用报销", "chunk_index": 2,
        "char_start": 7100, "char_end": 7200,
    },
]


async def main() -> None:
    settings = get_settings()

    say("=" * 70)
    say("真实 RAG 底层验证")
    say("=" * 70)
    say(f"  embed_model  = {settings.embed_model}")
    say(f"  embed_dim    = {settings.embed_dim}")
    say(f"  qdrant_url   = {settings.qdrant_url}")
    say(f"  collection   = {settings.qdrant_collection}")

    # ---- 1. 编码器 ----
    say("")
    say("-" * 70)
    say("1. Embedder 加载与维度自检（首次会下载模型）")
    say("-" * 70)

    embedder = Embedder.from_settings(settings)
    t0 = time.perf_counter()
    try:
        info = await embedder.warmup()
        elapsed = int((time.perf_counter() - t0) * 1000)
        say(f"  model       = {info['model']}")
        say(f"  期望维度    = {info['expected_dim']}")
        say(f"  实际维度    = {info['actual_dim']}")
        say(f"  维度匹配    = {info['dim_ok']}")
        say(f"  首次加载耗时 = {elapsed} ms（含可能的模型下载）")
    except Exception as exc:  # noqa: BLE001
        say(f"  [FAIL] {type(exc).__name__}: {exc}")
        Path(OUT).write_text("\n".join(lines), encoding="utf-8")
        print("fail: embedder")
        raise SystemExit(1)

    # ---- 2. Qdrant ----
    say("")
    say("-" * 70)
    say("2. Qdrant collection")
    say("-" * 70)

    store = QdrantStore.from_settings(settings)
    try:
        health = await store.health()
        say(f"  url          = {health['url']}")
        say(f"  collection   = {health['collection']}")
        say(f"  dim          = {health['dim']}")
        say(f"  本次新建     = {health['created_now']}")
        say(f"  已有 points  = {health['points']}")
    except Exception as exc:  # noqa: BLE001
        say(f"  [FAIL] {type(exc).__name__}: {exc}")
        Path(OUT).write_text("\n".join(lines), encoding="utf-8")
        print("fail: qdrant")
        raise SystemExit(1)

    # ---- 3. 写入 ----
    say("")
    say("-" * 70)
    say("3. 编码 3 条测试片段并写入")
    say("-" * 70)

    try:
        t1 = time.perf_counter()
        vectors = await embedder.embed_passages([c["text"] for c in TEST_CHUNKS])
        embed_ms = int((time.perf_counter() - t1) * 1000)
        say(f"  编码 3 条耗时 = {embed_ms} ms")
        say(f"  向量维度      = {len(vectors[0])}")
        say(f"  前 3 个分量   = {[round(x, 4) for x in vectors[0][:3]]}")

        n = await store.upsert_chunks(
            doc_id="demo-doc-001",
            doc_name="员工手册.pdf",
            chunks=TEST_CHUNKS,
            vectors=vectors,
        )
        say(f"  写入成功      = {n} 条")

        # 幂等性：再写一次应该不增加数量
        await store.upsert_chunks(
            doc_id="demo-doc-001",
            doc_name="员工手册.pdf",
            chunks=TEST_CHUNKS,
            vectors=vectors,
        )
        total = await store.count()
        say(f"  重复写入后总数 = {total}（应与上面相同，uuid5 幂等）")
    except Exception as exc:  # noqa: BLE001
        say(f"  [FAIL] {type(exc).__name__}: {exc}")
        Path(OUT).write_text("\n".join(lines), encoding="utf-8")
        print("fail: upsert")
        raise SystemExit(1)

    # ---- 4. 检索 ----
    say("")
    say("-" * 70)
    say("4. 检索")
    say("-" * 70)

    for question in ("试用期员工能休年假吗？", "差旅费怎么报销？"):
        try:
            t2 = time.perf_counter()
            qv = await embedder.embed_query(question)
            hits = await store.search(qv, top_k=3)
            search_ms = int((time.perf_counter() - t2) * 1000)

            say("")
            say(f"  Q: {question}   （{search_ms} ms）")
            for rank, hit in enumerate(hits, start=1):
                score = hit["score"]
                score_str = f"{score:.4f}" if score is not None else "None"
                say(f"    #{rank} score={score_str} p.{hit['page_no']} "
                    f"[{hit['section_title']}]")
                say(f"        {hit['text'][:50]}...")
        except Exception as exc:  # noqa: BLE001
            say(f"  [FAIL] 检索 {question}: {type(exc).__name__}: {exc}")

    # ---- 5. 清理测试数据 ----
    say("")
    say("-" * 70)
    say("5. 清理测试数据（按 doc_id 删除）")
    say("-" * 70)
    try:
        await store.delete_document("demo-doc-001")
        say(f"  删除后剩余 points = {await store.count()}")
    except Exception as exc:  # noqa: BLE001
        say(f"  [WARN] 清理失败: {type(exc).__name__}: {exc}")

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
