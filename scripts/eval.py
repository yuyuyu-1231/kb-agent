r"""scripts/eval.py — 跑评估集，算四层指标，出报告。

用法：
    python scripts/eval.py                 # 全量 50 条
    python scripts/eval.py --limit 5       # 只跑前 5 条（验证脚本用）

产出：
    data/reports/{YYYYMMDD-HHMM}.json   完整指标 + 逐条明细
    data/reports/{YYYYMMDD-HHMM}.md     可读报告

三个设计决策：

1. **L1 检索单独跑一次，不复用 Agent 的 trace**
   trace 的 observation 只存 digest + 前 120 字符，还原不出完整 chunk 列表。
   而且 L1 要测的是「检索器本身」，应该与 Agent 是否改写 query 解耦 ——
   否则分不清是检索器差还是 query 改写差。

2. **每题独立 session_id**
   否则第 1 题的对话会进第 2 题的上下文，后面的分数全被污染。

3. **工单写独立的 data/eval_tickets.db，跑前清空**
   演示用的 data/kb.db 不能动；清空是为了让「本次建单数」这个数字准确。
   用 DELETE 而不是删文件，避免留下 -wal/-shm 残留。
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings                    # noqa: E402
from app.infra.embedding import Embedder                    # noqa: E402
from app.infra.llm import OpenAILLMClient                   # noqa: E402
from app.infra.qdrant import QdrantStore                    # noqa: E402
from app.services.evaluation import (                       # noqa: E402
    ItemResult,
    aggregate,
    aggregate_by_type,
    citation_matches,
    detect_refusal,
    detect_safety_refusal,
    find_must_not,
    match_key_point,
)
from app.services.retrieval import Retriever                # noqa: E402
from app.services.ticket import TicketStore                 # noqa: E402
from app.tools.knowledge import register_knowledge_tools    # noqa: E402
from app.tools.misc import register_misc_tools              # noqa: E402
from app.tools.ticket import register_ticket_tools          # noqa: E402
from harness.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop   # noqa: E402
from harness.tools import ToolRegistry                      # noqa: E402
from harness.trace import replay                            # noqa: E402

EVAL_SET_PATH = ROOT / "data" / "eval_set.jsonl"
REPORT_DIR = ROOT / "data" / "reports"
EVAL_TICKET_DB = ROOT / "data" / "eval_tickets.db"

# 三份评估集分开跑：
# - main   主集 50 条，测基础能力
# - robust 变体 45 条，测“换个问法还答不答对”
# - safety 安全 6 条，测“给了攻击指令会不会照做”
# 不合并的原因：安全题没有 expected_sources，混进同一份 recall 会拉低分母。
SETS = {
    "main": ROOT / "data" / "eval_set.jsonl",
    "robust": ROOT / "data" / "eval_robust.jsonl",
    "safety": ROOT / "data" / "eval_safety.jsonl",
}

# 评估用的 prompt 直接复用 harness 的默认值。
# 两处各维护一份会让“评估跑的行为”和“上线跑的行为”分叉，
# 那样指标就不再描述真实系统。
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# 准备
# --------------------------------------------------------------------------- #

def load_eval_set(set_name: str, limit: int | None = None) -> list[dict]:
    """读评估集。set_name 为 all 时按 main → robust → safety 顺序拼接。"""
    names = list(SETS) if set_name == "all" else [set_name]
    items: list[dict] = []
    for name in names:
        path = SETS[name]
        if not path.exists():
            raise SystemExit(f"[错误] 评估集不存在: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items[:limit] if limit else items


def reset_tickets(store: TicketStore) -> None:
    """清空评估专用工单库。"""
    conn = sqlite3.connect(store.db_path, timeout=5.0)
    try:
        with conn:
            conn.execute("DELETE FROM tickets")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 单条评估
# --------------------------------------------------------------------------- #

async def evaluate_one(
    item: dict,
    loop: AgentLoop,
    retriever: Retriever,
    trace_dir: str,
) -> ItemResult:
    r = ItemResult(
        item_id=item["id"],
        item_type=item["type"],
        answerable=item["answerable"],
        expected_action=item["expected_action"],
        question=item["question"],
    )
    expected_pages = [s["page_no"] for s in item["expected_sources"]]
    expected_chunks = [s["chunk_index"] for s in item["expected_sources"]]
    r.expected_pages = expected_pages

    # ---- L1：单独跑检索，测检索器本身 ----
    try:
        hits = await retriever.search(item["question"], top_k=5)
        r.retrieved_chunks = [
            h["chunk_index"] for h in hits if h.get("chunk_index") is not None
        ]
    except Exception as exc:  # noqa: BLE001
        r.error = f"retrieval: {type(exc).__name__}: {exc}"

    if expected_chunks:
        for rank, ci in enumerate(r.retrieved_chunks, start=1):
            if ci in expected_chunks:
                r.recall_hit = True
                r.reciprocal_rank = 1.0 / rank
                break

    # ---- 跑 Agent ----
    try:
        result = await loop.run(item["question"], session_id=f"eval_{item['id']}")
    except Exception as exc:  # noqa: BLE001
        r.error = f"agent: {type(exc).__name__}: {exc}"
        return r

    r.answer = result.answer
    r.stop_reason = result.stop_reason.value
    r.steps = result.steps
    r.latency_ms = result.latency_ms
    r.tokens = result.tokens
    r.ticket_id = result.ticket_id
    r.cited_pages = [c.page_no for c in result.citations if c.page_no is not None]

    # ---- L2 ----
    if expected_chunks:
        r.citation_ok = citation_matches(r.cited_pages, expected_pages)
    # refused 只表示「语言上表达了找不到」，不表示「最终判定为拒答」。
    # 具体算不算误拒由 aggregate() 结合 citation_ok 判断，这样同一个字段
    # 能同时支撑两个语义不同的指标（应拒答率 / 误拒率）。
    #
    # 安全题用另一套话术（“无法提供/无权” vs “未找到/未收录”），
    # 必须换 marker，否则安全题会全被判成没拒答。
    if item.get("type") == "safety":
        r.refused = detect_safety_refusal(r.answer)
    else:
        r.refused = detect_refusal(r.answer)
    r.must_not_hits = find_must_not(r.answer, item.get("must_not") or [])

    key_points = item.get("key_points") or []
    r.keypoints_total = len(key_points)
    r.keypoints_hit = sum(1 for kp in key_points if match_key_point(r.answer, kp))

    # ---- L3 ----
    if item.get("type") == "safety":
        # 安全题的动作正确 = 安全通过 = **没泄露**。
        # 不把“是否明确拒绝”算进来：模型话术千变万化，关键词匹配不可靠，
        # 而且“没拒绝但也没泄露”不构成安全事故。拒绝率单独统计。
        r.action_correct = not r.must_not_hits
    else:
        r.action_correct = (
            r.stop_reason == "ticket_created"
            if item["expected_action"] == "ticket"
            else r.stop_reason == "answered"
        )

    try:
        records = replay(trace_dir, result.trace_id)
        r.tool_sequence = [
            str(rec.get("tool"))
            for rec in records
            if rec.get("tool") and not str(rec.get("tool")).startswith("__")
        ]
    except Exception:  # noqa: BLE001
        pass

    return r


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def render_markdown(report: dict) -> str:
    s = report["summary"]
    cfg = report["config"]
    lines: list[str] = []

    lines.append("# 评估报告")
    lines.append("")
    lines.append(
        f"运行时间：{report['run_at']}　总耗时：{report['elapsed_seconds']}s　"
        f"样本：{report['config']['eval_set_size']} 条"
    )
    lines.append("")
    lines.append(
        f"配置：模型 `{cfg['model']}`　chunk `{cfg['chunk_size']}/{cfg['chunk_overlap']}`　"
        f"top_k `{cfg['top_k']}`　max_steps `{cfg['max_steps']}`"
    )
    lines.append("")
    lines.append("## 四层指标")
    lines.append("")
    lines.append("| 层 | 指标 | 值 | 说明 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| L1 | Recall@5 | **{s['L1_recall_at_5']:.1%}** | 正确 chunk 进 top5 的比例 |")
    lines.append(f"| L1 | MRR | {s['L1_mrr']:.3f} | 正确 chunk 的平均倒数排名 |")
    lines.append(f"| L2 | 引用准确率 | **{s['L2_citation_precision']:.1%}** | 引用页码命中期望页码 |")
    lines.append(f"| L2 | 应拒答率 | **{s['L2_refusal_rate']:.1%}** | 库外问题明确拒答的比例 |")
    lines.append(f"| L2 | 误拒率 | {s['L2_false_refusal_rate']:.1%} | 可答题被错误拒答的比例 |")
    lines.append(f"| L2 | 关键点覆盖 | {s['L2_keypoint_coverage']:.1%} | key_points 被答案覆盖的比例 |")
    lines.append(
        f"| L2 | 幻觉条数 | **{s['L2_hallucination_count']}** "
        f"({s['L2_hallucination_rate']:.1%}) | 出现 must_not 词条的题数 |"
    )
    lines.append(f"| L3 | 动作准确率 | **{s['L3_action_accuracy']:.1%}** | 该答就答 / 该转就转 |")
    lines.append(f"| L3 | 平均步数 | {s['L3_avg_steps']} | 每题的 LLM 调用轮数 |")
    lines.append(f"| L4 | 延迟 P50 | {s['L4_latency_p50_ms']:.0f} ms | |")
    lines.append(f"| L4 | 延迟 P95 | **{s['L4_latency_p95_ms']:.0f} ms** | |")
    lines.append(f"| L4 | 平均 token | {s['L4_avg_tokens']:.0f} | |")
    lines.append(
        f"| L4 | 无效成本占比 | **{s['L4_wasted_cost_ratio']:.1%}** "
        f"（{s['L4_wasted_tokens']} token） | loop_detected + max_steps 烧掉的 |"
    )
    lines.append("")

    lines.append("## 按题型分组")
    lines.append("")
    lines.append("| 类型 | 条数 | Recall@5 | 引用准确 | 关键点覆盖 | 动作准确 | 平均步数 | 平均延迟 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    label = {"direct": "直接事实", "multi_hop": "跨段落多跳", "refusal": "应拒答"}
    for tkey, v in report["by_type"].items():
        def fmt(x, pct=True):
            if x is None:
                return "—"
            return f"{x:.1%}" if pct else f"{x}"
        lines.append(
            f"| {label.get(tkey, tkey)} | {v['count']} | "
            f"{fmt(v['recall_at_5'])} | {fmt(v['citation_precision'])} | "
            f"{fmt(v['keypoint_coverage'])} | {fmt(v['action_accuracy'])} | "
            f"{v['avg_steps']} | {v['avg_latency_ms']:.0f}ms |"
        )
    lines.append("")

    lines.append("## 成本按终止原因拆分")
    lines.append("")
    lines.append("| stop_reason | 条数 | 平均 token | 平均延迟 |")
    lines.append("|---|---|---|---|")
    for reason, v in sorted(
        s["L4_cost_by_stop_reason"].items(), key=lambda kv: -kv[1]["count"]
    ):
        lines.append(
            f"| `{reason}` | {v['count']} | {v['avg_tokens']:.0f} | {v['avg_latency_ms']:.0f}ms |"
        )
    lines.append("")

    lines.append("## 需要关注的题")
    lines.append("")
    issues_rows: list[str] = []
    for it in report["items"]:
        issues: list[str] = []
        if it["must_not_hits"]:
            issues.append("幻觉：" + " / ".join(it["must_not_hits"]))
        if it["answerable"] != "none":
            if not it["recall_hit"]:
                issues.append("检索未命中")
            if not it["citation_ok"]:
                issues.append("引用不准")
            if it["refused"]:
                issues.append("误拒答")
        if not it["action_correct"]:
            issues.append(f"动作错（期望 {it['expected_action']}，实际 {it['stop_reason']}）")
        if it["error"]:
            issues.append("异常：" + it["error"][:60])
        if issues:
            issues_rows.append(
                f"| {it['id']} | {it['question'][:28]} | {'；'.join(issues)} |"
            )

    if issues_rows:
        lines.append("| ID | 问题 | 现象 |")
        lines.append("|---|---|---|")
        lines.extend(issues_rows)
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 逐条明细")
    lines.append("")
    lines.append("| ID | stop | 步数 | token | 延迟 | recall | 引用 | 关键点 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for it in report["items"]:
        lines.append(
            f"| {it['id']} | `{it['stop_reason']}` | {it['steps']} | {it['tokens']} | "
            f"{it['latency_ms']}ms | {'✓' if it['recall_hit'] else '✗'} | "
            f"{'✓' if it['citation_ok'] else '✗'} | "
            f"{it['keypoints_hit']}/{it['keypoints_total']} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

class _Tee:
    """把 stdout 同时写到文件和屏幕。

    后台跑 50 条时看不到终端，靠 data/reports/last_run.log 跟进度。
    用 Python 自己写文件而不是 PowerShell 重定向，是因为后者会用
    GBK 重写 UTF-8 字节，中文全变乱码。
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass


async def main(limit: int | None, set_name: str) -> None:
    settings = get_settings()
    items = load_eval_set(set_name, limit)
    total = len(items)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # buffering=1 是行缓冲：每写一行就刷盘。
    # 默认块缓冲会让后台跑时日志文件一直 0 字节（实测踩过）。
    log_fh = open(REPORT_DIR / "last_run.log", "w", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, log_fh)

    print("=" * 72)
    print(f"评估开始　集合 {set_name}　样本 {total} 条　模型 {settings.llm_model_id}")
    print("=" * 72)

    embedder = Embedder.from_settings(settings)
    vector_store = QdrantStore.from_settings(settings)
    await vector_store.ensure_collection()
    retriever = Retriever.from_infra(embedder, vector_store)

    ticket_store = TicketStore(EVAL_TICKET_DB)
    reset_tickets(ticket_store)

    registry = ToolRegistry()
    register_knowledge_tools(registry, retriever)
    register_ticket_tools(registry, ticket_store)
    register_misc_tools(registry)

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

    results: list[ItemResult] = []
    started = time.perf_counter()

    for idx, item in enumerate(items, start=1):
        t0 = time.perf_counter()
        r = await evaluate_one(item, loop, retriever, str(settings.trace_dir))
        elapsed = int((time.perf_counter() - t0) * 1000)
        results.append(r)

        flag = "OK" if r.action_correct else "XX"
        print(
            f"[{idx:>2}/{total}] {r.item_id:<4} {flag} "
            f"stop={r.stop_reason:<15} steps={r.steps} "
            f"kpt={r.keypoints_hit}/{r.keypoints_total} "
            f"recall={int(r.recall_hit)} cite={int(r.citation_ok)} "
            f"{elapsed}ms"
        )

    elapsed_total = int(time.perf_counter() - started)
    print("")
    print(f"跑完 {total} 条，用时 {elapsed_total}s")

    report = {
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "model": settings.llm_model_id,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "max_steps": settings.max_steps,
            "token_budget": settings.token_budget,
            "top_k": 5,
            "eval_set_size": total,
            # 工具数量记进报告：4 → 6 的对比靠这个字段区分
            "tool_count": len(registry),
            "tools": registry.names(),
        },
        "summary": aggregate(results),
        "by_type": aggregate_by_type(results),
        "tickets_created": len(ticket_store.list(limit=500)),
        "elapsed_seconds": elapsed_total,
        "items": [r.to_dict() for r in results],
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    json_path = REPORT_DIR / f"{stamp}-{set_name}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_path = REPORT_DIR / f"{stamp}-{set_name}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")

    s = report["summary"]
    print("")
    print(f"Recall@5 {s['L1_recall_at_5']:.1%}　"
          f"引用准确 {s['L2_citation_precision']:.1%}　"
          f"应拒答 {s['L2_refusal_rate']:.1%}　"
          f"动作准确 {s['L3_action_accuracy']:.1%}")
    if s.get("L2_safety_pass_rate") is not None:
        print(f"安全通过率 {s['L2_safety_pass_rate']:.1%}　"
              f"明确拒绝 {s['L2_safety_refusal_rate']:.1%}　"
              f"（{s['L2_safety_count']} 条安全题）")
    print(f"P95 {s['L4_latency_p95_ms']:.0f}ms　"
          f"平均 {s['L4_avg_tokens']:.0f} token　"
          f"无效成本 {s['L4_wasted_cost_ratio']:.1%}")
    print(f"本次建单 {report['tickets_created']} 张")
    print("")
    print(f"-> {json_path}")
    print(f"-> {md_path}")

    await llm.aclose()
    await vector_store.close()

    sys.stdout = sys.__stdout__
    log_fh.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument(
        "--set",
        dest="set_name",
        choices=[*SETS, "all"],
        default="main",
        help="跑哪份评估集（默认 main）",
    )
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.set_name))
