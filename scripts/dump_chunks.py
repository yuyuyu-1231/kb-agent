r"""把 14 个 chunk 的完整内容导出到 docs/CORPUS.md。

两个用途：
1. **审核评估集时对照原文** —— `data/eval_set.jsonl` 里 50 条题的
   expected_sources 都能在这里逐字核对，不用打开 PDF。
2. **展示语料处理结果** —— 6 页 PDF 是怎样被切成 14 个 chunk 的，
   一眼能看到清洗和分块的边界。

输出：docs/CORPUS.md
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ingest import parse_pdf  # noqa: E402

PDF = ROOT / "data" / "corpus" / "员工手册.pdf"
OUT = ROOT / "docs" / "CORPUS.md"


def main() -> None:
    chunks, result = parse_pdf(PDF, chunk_size=400, overlap=80)

    lines: list[str] = []
    lines.append("# 语料全文（供评估集比对）")
    lines.append("")
    lines.append(f"源文件：`data/corpus/{result.doc_name}`")
    lines.append("")
    lines.append(
        f"{result.pages} 页 → 提取 {result.blocks + result.dropped_margin_blocks} 个文本块"
        f" → 剔除页眉页脚 {result.dropped_margin_blocks} 块"
        f" → 合并后 {result.blocks} 块"
        f" → 切成 **{result.chunks} 个 chunk**"
    )
    lines.append("")
    lines.append("> 审核 `data/eval_set.jsonl` 时，用这里的 `chunk N` 编号和页码对照。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| chunk | 页码 | 章节 | 字符数 | 开头 |")
    lines.append("|---|---|---|---|---|")
    for c in chunks:
        head = c["text"][:30].replace("\n", " ").replace("|", "丨")
        section = c["section_title"] or "(无)"
        lines.append(
            f"| {c['chunk_index']} | p.{c['page_no']} | {section} | "
            f"{len(c['text'])} | {head}… |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    for c in chunks:
        section = c["section_title"] or "(无)"
        lines.append(f"## chunk {c['chunk_index']} · p.{c['page_no']} · {section}")
        lines.append("")
        lines.append(
            f"字符范围 `[{c['char_start']}, {c['char_end']}]`，长度 {len(c['text'])}"
        )
        lines.append("")
        lines.append("```text")
        lines.append(c["text"])
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT}  ({len(chunks)} chunks)")


if __name__ == "__main__":
    main()
