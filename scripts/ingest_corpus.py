r"""把 data/corpus 下的所有 PDF 入库（幂等）。

## 为什么不复用 main.py 里的 `_auto_ingest`

那个只在「collection 为空」时跑（`count() > 0` 直接返回），
适合首次部署。但扩语料是**增量**场景：库里已有 14 个点，
新加 3 份文档时它什么都不做。

这里对每份文档先 `delete_document` 再 `upsert_chunks`，
所以重复跑不会产生重复点，也不会残留旧版本多出来的 chunk。

跑法：
    python scripts/ingest_corpus.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infra.embedding import Embedder  # noqa: E402
from app.infra.qdrant import QdrantStore  # noqa: E402
from app.services.ingest import parse_pdf  # noqa: E402


async def main() -> int:
    settings = get_settings()
    corpus = Path(settings.data_dir) / "corpus"
    pdfs = sorted(corpus.glob("*.pdf"))

    if not pdfs:
        print(f"{corpus} 下没有 PDF")
        return 1

    embedder = Embedder.from_settings(settings)
    store = QdrantStore.from_settings(settings)
    await store.ensure_collection()

    print(f"库内现有 points = {await store.count()}\n")

    for pdf in pdfs:
        t0 = time.perf_counter()
        chunks, parsed = await asyncio.to_thread(
            parse_pdf, pdf, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap
        )
        if not chunks:
            print(f"{pdf.name}: 没提取到文本，跳过")
            continue

        vectors = await embedder.embed_passages([c["text"] for c in chunks])
        await store.delete_document(pdf.stem)
        await store.upsert_chunks(
            doc_id=pdf.stem,
            doc_name=pdf.name,
            chunks=chunks,
            vectors=vectors,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"{pdf.name}: {parsed.pages} 页 → {len(chunks)} chunks  ({ms} ms)")

    total = await store.count()
    print(f"\n库内总 points = {total}")
    await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
