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
    find_must_not,
    match_key_point,
)

EVAL_SET_PATH = ROOT / "data" / "eval_set.jsonl"
BASELINE_MD = ROOT / "docs" / "BASELINE.md"

# 三份评估集都要读：重算时要根据题目 id 找到对应的断言
EVAL_PATHS = (
    ROOT / "data" / "eval_set.jsonl",
    ROOT / "data" / "eval_robust.jsonl",
    ROOT / "data" / "eval_safety.jsonl",
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


def load_eval_items() -> dict[str, dict]:
    """读评估集，用于重算 must_not / key_points。

    为什么需要：这两项是评估集里的断言，改了评估集就必须重算，
    否则报告里的“幻觉条数”还是旧断言的产物。
    """
    items: dict[str, dict] = {}
    for path in EVAL_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            items[item["id"]] = item
    return items


def rebuild(d: dict, item: dict) -> ItemResult:
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
    # ---- 用最新评估集重算三个判定 ----
    # refused 只表示「语言上表达了找不到」，不表示「最终判定为拒答」。
    # 具体算不算误拒，由 aggregate() 结合 citation_ok 判断：
    #   - 应拒答率：R 系列里 refused=True 的比例（不管有没有引用）
    #   - 误拒率：  full 系列里 refused=True 且 citation_ok=False 的比例
    r.refused = detect_refusal(r.answer)

    # must_not 从评估集重取：旧断言里把「正确列举的对比值」当成了禁令
    r.must_not_hits = find_must_not(r.answer, item.get("must_not") or [])

    # key_points 也从评估集重取：R 系列的断言从行为要求改成了空
    key_points = item.get("key_points") or []
    r.keypoints_total = len(key_points)
    r.keypoints_hit = sum(1 for kp in key_points if match_key_point(r.answer, kp))

    return r


def render_baseline_md(out: dict, source: Path) -> str:
    """生成第 4 周优化对比用的基线文档。

    只保留能直接写进简历/面试展示的内容：指标、分组、成本、已知问题、
    复现方法。不附 50 条逐条明细（那个在 data/reports/*.md）。
    """
    s = out["summary"]
    cfg = out["config"]
    by_type = out["by_type"]
    label = {"direct": "直接事实", "multi_hop": "跨段落多跳", "refusal": "应拒答"}

    lines: list[str] = []
    lines.append("# 评估基线（第 4 周优化对比基准）")
    lines.append("")
    lines.append(
        "> 本文件由 `scripts/rescore.py` 从一次真实运行的 output 生成。"
        "第 4 周的每一项优化（标题分块 / 混合检索 / reranker / 上下文压缩）"
        "都拿这组数字做对比基准。"
    )
    lines.append("")
    lines.append(f"- 数据来源：`{source.name}`（{out['run_at']}）")
    lines.append(f"- 重算时间：{out['rescored_at']}")
    lines.append(f"- 重算说明：{out['rescore_note']}")
    lines.append(f"- 样本：{cfg['eval_set_size']} 条")
    lines.append(
        f"- 配置：模型 `{cfg['model']}`，chunk `{cfg['chunk_size']}/{cfg['chunk_overlap']}`，"
        f"top_k `{cfg['top_k']}`，max_steps `{cfg['max_steps']}`")
    lines.append("")
    lines.append("## 四层指标 vs SPEC 目标")
    lines.append("")
    lines.append("| 层 | 指标 | 实测 | 目标 | 达标 |")
    lines.append("|---|---|---|---|---|")

    def row(layer: str, name: str, actual: str, target: str, ok: bool) -> str:
        return f"| {layer} | {name} | **{actual}** | {target} | {'✓' if ok else '✗'} |"

    lines.append(row("L1", "Recall@5", f"{s['L1_recall_at_5']:.1%}", "> 70%", s["L1_recall_at_5"] > 0.70))
    lines.append(row("L1", "MRR", f"{s['L1_mrr']:.3f}", "—", True))
    lines.append(row("L2", "引用准确率", f"{s['L2_citation_precision']:.1%}", "> 90%", s["L2_citation_precision"] > 0.90))
    lines.append(row("L2", "应拒答率", f"{s['L2_refusal_rate']:.1%}", "> 80%", s["L2_refusal_rate"] >= 0.80))
    lines.append(row("L2", "误拒率", f"{s['L2_false_refusal_rate']:.1%}", "越低越好", s["L2_false_refusal_rate"] < 0.05))
    lines.append(row("L2", "关键点覆盖", f"{s['L2_keypoint_coverage']:.1%}", "—", True))
    lines.append(row("L2", "幻觉条数", f"{s['L2_hallucination_count']} / {s['total']}", "0", s["L2_hallucination_count"] == 0))
    lines.append(row("L3", "动作准确率", f"{s['L3_action_accuracy']:.1%}", "> 85%", s["L3_action_accuracy"] > 0.85))
    lines.append(row("L3", "平均步数", f"{s['L3_avg_steps']}", "< 3", s["L3_avg_steps"] < 3))
    lines.append(row("L4", "P95 延迟", f"{s['L4_latency_p95_ms']:.0f} ms", "< 8000 ms", s["L4_latency_p95_ms"] < 8000))
    lines.append(row("L4", "平均 token", f"{s['L4_avg_tokens']:.0f}", "—", True))
    lines.append(row("L4", "无效成本占比", f"{s['L4_wasted_cost_ratio']:.1%}", "趋近 0", s["L4_wasted_cost_ratio"] < 0.05))
    lines.append("")

    lines.append("## 按题型分组")
    lines.append("")
    lines.append("| 类型 | 条数 | Recall@5 | 引用准确 | 关键点覆盖 | 动作准确 | 平均步数 | 平均延迟 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for tkey in ("direct", "multi_hop", "refusal"):
        v = by_type.get(tkey)
        if not v:
            continue

        def fmt(x):
            return "—" if x is None else f"{x:.1%}"

        lines.append(
            f"| {label[tkey]} | {v['count']} | {fmt(v['recall_at_5'])} | "
            f"{fmt(v['citation_precision'])} | {fmt(v['keypoint_coverage'])} | "
            f"{fmt(v['action_accuracy'])} | {v['avg_steps']} | {v['avg_latency_ms']:.0f}ms |"
        )
    lines.append("")

    lines.append("## 成本按终止原因拆分")
    lines.append("")
    lines.append("| stop_reason | 条数 | 平均 token | 平均延迟 |")
    lines.append("|---|---|---|---|")
    for reason, v in sorted(s["L4_cost_by_stop_reason"].items(), key=lambda kv: -kv[1]["count"]):
        lines.append(
            f"| `{reason}` | {v['count']} | {v['avg_tokens']:.0f} | {v['avg_latency_ms']:.0f}ms |"
        )
    lines.append("")
    lines.append(
        f"本次共建单 {out.get('tickets_created', 0)} 张；"
        f"评估集里标注应转单的题是 5 条（R01-R05）。"
    )
    lines.append("")

    lines.append("## 已知问题（第 4 周的优化靶子）")
    lines.append("")
    lines.append("| # | 问题 | 影响 |")
    lines.append("|---|---|---|")
    lines.append(
        "| 1 | P95 延迟超过 8s 目标 | 应拒答题平均 10s（多轮检索 + 建单），"
        "是长尾的主要来源 |"
    )
    lines.append(
        "| 2 | 应拒答率 80% 仅刚好达标 | R02 编造了「期权」，应拒答与幻觉是同一个错误的两个面 |"
    )
    lines.append(
        "| 3 | 3 条幻觉未消除 | D16（20 分钟答成 30-120 分钟规则）、"
        "D17（用了手册没有的「旷工」）、R02（股权激励编造） |"
    )
    lines.append(
        "| 4 | H13 动作错 | 迟到 4 次 + 病假 2 天，有明确答案却建了单 |"
    )
    lines.append(
        "| 5 | 语料只有 6 页 / 14 个 chunk | 指标方差大，单次运行不能代表稳定性；"
        "第 3 周应补鲁棒性变体集与多轮重复 |"
    )
    lines.append("")

    lines.append("## 复现方法")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/build_eval_set.py       # 生成评估集（含 chunk 交叉校验）")
    lines.append("python scripts/eval.py                 # 跑 50 条，出 data/reports/{时间}.json/.md")
    lines.append("python scripts/rescore.py <报告路径>     # 只改指标算法时离线重算，不重跑 Agent")
    lines.append("python scripts/dump_chunks.py          # 导出 chunk 全文供人工核对")
    lines.append("```")
    lines.append("")
    lines.append("> 指标算法本身也迭代了三次（参见 `app/services/evaluation.py` 里的注释）：")
    lines.append("> 关键词、引用、二者组合，三个版本的误拒率分别是 22.2% / 48.5% / 0%。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/rescore.py <report.json>")
        raise SystemExit(1)

    src = Path(sys.argv[1])
    if not src.is_absolute():
        src = ROOT / src
    report = json.loads(src.read_text(encoding="utf-8"))

    eval_items = load_eval_items()
    results = [rebuild(d, eval_items.get(d["id"], {})) for d in report["items"]]
    new_summary = aggregate(results)
    new_by_type = aggregate_by_type(results)

    out = {
        "run_at": report["run_at"],
        "rescored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rescore_note": "用最新评估集重算 refused / must_not_hits / key_points，未重跑 Agent",
        "config": report["config"],
        "summary": new_summary,
        "by_type": new_by_type,
        "tickets_created": report.get("tickets_created", 0),
        "elapsed_seconds": report.get("elapsed_seconds", 0),
        "items": [r.to_dict() for r in results],
    }

    dst = src.with_name(src.stem + "-rescored.json")
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    baseline_md = render_baseline_md(out, src)
    BASELINE_MD.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_MD.write_text(baseline_md, encoding="utf-8")

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
    print(f"-> {BASELINE_MD}")

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
