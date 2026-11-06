r"""导出全部 chunk 的完整内容，用于人工出评估题。

出题必须基于真实 chunk 文本，凭概览猜会标错 chunk_index 和标准答案。
输出 E:\DevCaches\kb_chunks_dump.txt
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ingest import parse_pdf  # noqa: E402

PDF = ROOT / "data" / "corpus" / "员工手册.pdf"
OUT = r"E:\DevCaches\kb_chunks_dump.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)


chunks, result = parse_pdf(PDF, chunk_size=400, overlap=80)

say("=" * 78)
say(f"文档: {result.doc_name}   页数: {result.pages}   块数: {result.blocks}   chunk: {result.chunks}")
say("=" * 78)
say("")

for c in chunks:
    say("-" * 78)
    say(f"[chunk_index={c['chunk_index']}]  page_no={c['page_no']}  section={c['section_title'] or '(无)'}")
    say(f"char_range=[{c['char_start']}, {c['char_end']}]  length={len(c['text'])}")
    say("-" * 78)
    say(c["text"])
    say("")

Path(OUT).write_text("\n".join(lines), encoding="utf-8")
print(f"written: {OUT}  ({len(chunks)} chunks)")
