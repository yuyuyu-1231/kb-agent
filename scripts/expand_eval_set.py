r"""把新增的 50 条题目合并进评估集，并做交叉校验。

## 校验什么

1. **chunk_index 存在**：每个 doc 实际解析出来的 chunk 数量是上限
2. **page_no 对得上**：标了 p3，那个 chunk 真的在第 3 页
3. **section_title 逐字符一致**：包括全角空格和标点
4. **结构性检查**：
   - 多跳题（multi_hop）至少 2 个来源
   - 冲突题（conflict）的来源必须跨至少两份文档
   - 拒答题（refusal）的 expected_sources 必须为空
   - 新 id 不能和现有 50 条重复

**为什么必须校验**：chunk_index 是手写的，page_no 是手写的，
章节标题里还有全角空格——任何一处写错，评估结果就会归因到错误的位置，
而且不会报错，只会让"引用准确率"莫名其妙变差。

跑法：
    python scripts/expand_eval_set.py            # 校验 + 写入
    python scripts/expand_eval_set.py --dry-run  # 只校验不写
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ingest import parse_pdf  # noqa: E402
from scripts.eval_expand_data import ALL_NEW  # noqa: E402

CORPUS = ROOT / "data" / "corpus"
EVAL_SET = ROOT / "data" / "eval_set.jsonl"


def load_corpus() -> dict[str, dict[int, dict]]:
    """doc_id -> {chunk_index: chunk}"""
    out: dict[str, dict[int, dict]] = {}
    for pdf in sorted(CORPUS.glob("*.pdf")):
        chunks, _ = parse_pdf(pdf, chunk_size=400, overlap=80)
        out[pdf.stem] = {c["chunk_index"]: c for c in chunks}
    return out


def validate(items: list[dict], corpus: dict[str, dict[int, dict]]) -> list[str]:
    problems: list[str] = []

    for item in items:
        iid = item["id"]
        srcs = item.get("expected_sources", [])

        # ---- 结构性 ----
        if item["type"] == "multi_hop" and len(srcs) < 2:
            problems.append(f"{iid}: multi_hop 只有 {len(srcs)} 个来源，至少要 2 个")
        if item["type"] == "conflict":
            docs = {s["doc_id"] for s in srcs}
            if len(docs) < 2:
                problems.append(f"{iid}: conflict 的来源只涉及 {docs}，必须跨文档")
        if item["type"] == "refusal" and srcs:
            problems.append(f"{iid}: refusal 不应该有 expected_sources")

        # ---- 逐源校验 ----
        for s in srcs:
            doc, ci = s["doc_id"], s["chunk_index"]
            if doc not in corpus:
                problems.append(f"{iid}: doc_id={doc!r} 不在语料里")
                continue
            by_idx = corpus[doc]
            real = by_idx.get(ci)
            if real is None:
                problems.append(
                    f"{iid}: {doc} 没有 chunk {ci}（该文档共 {len(by_idx)} 条，索引 0-{len(by_idx)-1}）"
                )
                continue
            if real["page_no"] != s["page_no"]:
                problems.append(
                    f"{iid}: {doc} chunk {ci} 实际在 p{real['page_no']}，标了 p{s['page_no']}"
                )
            if real["section_title"] != s["section_title"]:
                problems.append(
                    f"{iid}: {doc} chunk {ci} 实际章节 {real['section_title']!r}，"
                    f"标了 {s['section_title']!r}"
                )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只校验不写入")
    args = parser.parse_args()

    existing = [
        json.loads(line)
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    existing_ids = {it["id"] for it in existing}

    dup = [it["id"] for it in ALL_NEW if it["id"] in existing_ids]
    if dup:
        print(f"[FAIL] id 与现有题目重复: {dup}")
        return 1

    corpus = load_corpus()
    print("语料规模：")
    for doc, by_idx in corpus.items():
        pages = max(c["page_no"] for c in by_idx.values())
        print(f"  {doc}: {len(by_idx)} chunks, {pages} 页")

    problems = validate(ALL_NEW, corpus)
    print()
    if problems:
        print(f"[FAIL] 校验不通过，{len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"[OK] {len(ALL_NEW)} 条全部通过交叉校验")

    if args.dry_run:
        print("（--dry-run，未写入）")
        return 0

    merged = existing + ALL_NEW
    EVAL_SET.write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in merged) + "\n",
        encoding="utf-8",
    )

    from collections import Counter

    dist = Counter(it["type"] for it in merged)
    print(f"\n写入 {EVAL_SET.name}：{len(existing)} → {len(merged)} 条")
    for t, n in sorted(dist.items()):
        print(f"  {t:<12} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
