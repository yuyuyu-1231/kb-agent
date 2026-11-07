r"""从已有评估报告重建指标，不重跑 Agent。

什么时候用：改了「指标算法」但没改「模型行为」时。

重跑 Agent 有两个问题：
1. 花时间和钱（50 条约 4 分钟）
2. 模型有随机性，重跑后数据变了，你就分不清指标的变化
   有多少是算法改的效果、多少是模型波动

离线重算能把变量控制在「只改算法」这一项上，对比才干净。

用法：
    python scripts/rescore.py data/reports/20261107-1627.json

产出：
    data/reports/{原名}-rescored.json
    并在控制台打印新旧关键指标对比
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.evaluation import (  # noqa: E402
    ItemResult,
    aggregate,
    aggregate_by_type,
    detect_refusal,
)

COMPARE_KEYS = (
    "L1_recall_at_5",
    "L2_citation_precision",
    "L2_refusal_rate",
    "L2_false_refusal_rate",
    "L2_keypoint_coverage",
    "L2_hallucination_count",
    "L3_action_accuracy",
    "L3_avg_steps",
    "L4_latency_p95_ms",
    "L4_avg_tokens",
    "L4_wasted_cost_ratio",
)


def rebuild(d: dict) -> ItemResult:
    """从报告 JSON 的一条记录重建 ItemResult。"""
    r = ItemResult(
        item_id=d["id"],
        item_type=d["type"],
        answerable=d["answerable"],
        expected_action=d["expected_action"],
        question=d["question"],
        retrieved_chunks=d.get("retrieved_chunks") or [],
        recall_hit=bool(d.get("recall_hit")),
        reciprocal_rank=float(d.get("reciprocal_rank") or 0.0),
        answer=d.get("answer", ""),
        cited_pages=d.get("cited_pages") or [],
        expected_pages=d.get("expected_pages") or [],
        citation_ok=bool(d.get("citation_ok")),
        keypoints_hit=int(d.get("keypoints_hit") or 0),
        keypoints_total=int(d.get("keypoints_total") or 0),
        must_not_hits=d.get("must_not_hits") or [],
        stop_reason=d.get("stop_reason", ""),
        steps=int(d.get("steps") or 0),
        tool_sequence=d.get("tool_sequence") or [],
        action_correct=bool(d.get("action_correct")),
        ticket_id=d.get("ticket_id"),
        latency_ms=int(d.get("latency_ms") or 0),
        tokens=int(d.get("tokens") or 0),
        error=d.get("error", ""),
    )
    # refused 只表示「语言上表达了找不到」，不表示「最终判定为拒答」。
    # 具体算不算误拒，由 aggregate() 结合 citation_ok 判断：
    #   - 应拒答率：R 系列里 refused=True 的比例（不管有没有引用）
    #   - 误拒率：  full 系列里 refused=True 且 citation_ok=False 的比例
    # 两个指标共用同一个字段但语义不同，必须分开组合。
    r.refused = detect_refusal(r.answer)
    return r


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/rescore.py <report.json>")
        raise SystemExit(1)

    src = Path(sys.argv[1])
    if not src.is_absolute():
        src = ROOT / src
    report = json.loads(src.read_text(encoding="utf-8"))

    results = [rebuild(d) for d in report["items"]]
    new_summary = aggregate(results)
    new_by_type = aggregate_by_type(results)

    out = {
        "run_at": report["run_at"],
        "rescored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rescore_note": "只改了 refused 判定（关键词 + 无引用），未重跑 Agent",
        "config": report["config"],
        "summary": new_summary,
        "by_type": new_by_type,
        "tickets_created": report.get("tickets_created", 0),
        "elapsed_seconds": report.get("elapsed_seconds", 0),
        "items": [r.to_dict() for r in results],
    }

    dst = src.with_name(src.stem + "-rescored.json")
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    old_summary = report["summary"]
    print("=" * 68)
    print("指标重算对比（同一批答案，只改 refused 判定）")
    print("=" * 68)
    print(f"{'指标':<28}{'旧':>12}{'新':>12}{'变化':>12}")
    print("-" * 68)
    for key in COMPARE_KEYS:
        o = old_summary.get(key)
        n = new_summary.get(key)
        if isinstance(o, float) or isinstance(n, float):
            os_, ns = f"{o}", f"{n}"
            try:
                delta = float(n) - float(o)
                ds = f"{delta:+.4f}"
            except (TypeError, ValueError):
                ds = "—"
        else:
            os_, ns, ds = f"{o}", f"{n}", "—"
        print(f"{key:<28}{os_:>12}{ns:>12}{ds:>12}")
    print("")
    print(f"-> {dst}")

    # 检查误拒：和 aggregate() 保持一致的口径
    # （表达了找不到 **且** 没引用到期望页码）
    bad = [
        r for r in results
        if r.answerable == "full" and r.refused and not r.citation_ok
    ]
    if bad:
        print("")
        print("仍被判为拒答的 full 题：")
        for r in bad:
            print(f"  {r.item_id}  cited={r.cited_pages}")
    else:
        print("")
        print("没有 full 题被误判为拒答。")


if __name__ == "__main__":
    main()
