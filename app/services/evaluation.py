"""app/services/evaluation.py — 四层评估。

为什么放在 services 层而不是 scripts：
- 指标计算是业务逻辑（可单测、可复用），不是一次性脚本
- scripts/eval.py 只负责"跑批 + 落盘"，算法在这里

四层指标（对应 SPEC 5.3-5.6）：

    L1 检索层    Recall@k / MRR        —— 检索器本身好不好
    L2 生成层    引用准确率 / 应拒答率 / 误拒率 / 关键点覆盖 —— 答案质量
    L3 轨迹层    工具选择 / 步骤数 / 转单准确率      —— Agent 决策过程
    L4 系统层    延迟分位 / 成本 / 无效成本占比       —— 工程健康度

设计要点：

1. **L1 单独跑一次检索，不复用 Agent 的 trace**
   trace 的 observation 只存 digest + 前 120 字符，还原不出完整 chunk 列表。
   而且 L1 要测的是"检索器本身"，应该与 Agent 是否改写 query 解耦。

2. **关键点覆盖用"子串命中"近似，不用 LLM 判分**
   精确判分需要 LLM-as-Judge，成本高且不稳定。
   v1 用 key_points 在答案里的子串出现率，作为粗粒度信号。
   它会把"答对了但换了说法"判低，所以只用来做**版本间对比**，
   不用来做绝对质量结论。

3. **must_not 命中直接判幻觉，比关键点覆盖更硬**
   因为 must_not 里的词都是"语料里没有、模型却编出来"的东西。
   出现即错，不需要判分技巧。

4. **无效成本占比**
   定义为 (loop_detected + max_steps) 的 token / 全部 token。
   这两个 stop_reason 说明 Agent 自己没收敛，烧掉的钱是纯浪费。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

# 判断"是否明确拒答"的关键词。宁可宽一点：
# 漏判会把"其实拒答了"算成"没拒答"，让应拒答率虚低，比虚高更保守。
REFUSAL_MARKERS = (
    "未找到", "未收录", "没有找到", "无相关", "未规定", "没有规定",
    "未提及", "没有提及", "知识库中", "无法回答", "未说明", "没有说明",
)

# 无效成本的两个 stop_reason
WASTED_STOP_REASONS = {"loop_detected", "max_steps"}


# --------------------------------------------------------------------------- #
# 单条结果
# --------------------------------------------------------------------------- #

@dataclass
class ItemResult:
    """一条评估题的完整结果。既存原始指标，也存判断依据。"""
    item_id: str
    item_type: str
    answerable: str
    expected_action: str
    question: str

    # L1
    retrieved_chunks: list[int] = field(default_factory=list)
    recall_hit: bool = False
    reciprocal_rank: float = 0.0

    # L2
    answer: str = ""
    cited_pages: list[int] = field(default_factory=list)
    expected_pages: list[int] = field(default_factory=list)
    citation_ok: bool = False
    keypoints_hit: int = 0
    keypoints_total: int = 0
    must_not_hits: list[str] = field(default_factory=list)
    refused: bool = False

    # L3
    stop_reason: str = ""
    steps: int = 0
    tool_sequence: list[str] = field(default_factory=list)
    action_correct: bool = False
    ticket_id: str | None = None

    # L4
    latency_ms: int = 0
    tokens: int = 0

    # 诊断
    error: str = ""

    @property
    def keypoint_coverage(self) -> float:
        if self.keypoints_total == 0:
            return 0.0
        return self.keypoints_hit / self.keypoints_total

    @property
    def hallucinated(self) -> bool:
        return bool(self.must_not_hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "type": self.item_type,
            "answerable": self.answerable,
            "expected_action": self.expected_action,
            "question": self.question,
            # 存答案（截断）：否则报告里只有分数，人工复核时不知道模型到底答了什么
            "answer": self.answer[:600],
            "recall_hit": self.recall_hit,
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "retrieved_chunks": self.retrieved_chunks,
            "citation_ok": self.citation_ok,
            "cited_pages": self.cited_pages,
            "expected_pages": self.expected_pages,
            "keypoints_hit": self.keypoints_hit,
            "keypoints_total": self.keypoints_total,
            "keypoint_coverage": round(self.keypoint_coverage, 4),
            "must_not_hits": self.must_not_hits,
            "refused": self.refused,
            "stop_reason": self.stop_reason,
            "steps": self.steps,
            "tool_sequence": self.tool_sequence,
            "action_correct": self.action_correct,
            "ticket_id": self.ticket_id,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 判断函数（纯函数，可单测）
# --------------------------------------------------------------------------- #

def split_key_points(text: str) -> list[str]:
    """把 key_point 拆成可匹配的词元。

    例如 "5 天" -> ["5"]
         "二级以上医院" -> ["二级以上医院", "二级以"]
         "需转正后方可申请" -> ["需转正后方可申请", "需转正"]

    为什么要拆：模型很少逐字复述 key_point，但会说出其中的关键实体。
    整词匹配会把"答对了但换了说法"误判成没命中（实测 D02 因此只拿到 1/2）。
    """
    tokens: list[str] = [text]

    # 抽出连续的英数段（"5 天"里的 "5"、"80%"、"500 元"里的 "500"）
    for m in re.findall(r"[0-9]+(?:[.:][0-9]+)?%?", text):
        tokens.append(m)

    # 抽出长度 >= 2 的中文词组（用常见虚词切）
    for part in re.split(r"[，,。、；;：:\s（）()/]+", text):
        if len(part) >= 2 and part != text:
            tokens.append(part)

    # 长中文串：额外取前 3 字作为词元。
    # "需转正后方可申请" 的前缀是 "需转正"，能匹配到 "需转正后才能申请"。
    # 只取前缀不取后缀：后缀容易误匹配（"上医院" 这类）。
    cn_only = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if len(cn_only) >= 4:
        tokens.append(cn_only[:3])

    # 去掉纯单字和重复
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        t = t.strip()
        if len(t) < 1 or t in seen:
            continue
        seen.add(t)
        result.append(t)
    return result


def match_key_point(answer: str, key_point: str) -> bool:
    """key_point 是否被答案覆盖。

    策略：key_point 里任一"足够具体"的词元出现在答案里就算命中。
    "足够具体"定义为长度 >= 2，或者是一个数字。
    """
    if not answer:
        return False
    for token in split_key_points(key_point):
        if len(token) >= 2 or token.isdigit():
            if token in answer:
                return True
    return False


def detect_refusal(answer: str) -> bool:
    """答案是否明确表达了"找不到依据"。"""
    if not answer:
        return False
    return any(marker in answer for marker in REFUSAL_MARKERS)


def find_must_not(answer: str, must_not: list[str]) -> list[str]:
    """返回答案里出现的禁令词。出现即判幻觉。"""
    if not answer:
        return []
    return [m for m in must_not if m and m in answer]


def citation_matches(cited_pages: list[int], expected_pages: list[int]) -> bool:
    """至少有一条引用命中期望页码。

    为什么用页码而不是 chunk_index：
    - 引用对外暴露的是页码（AgentResult.citations[].page_no）
    - chunk_index 是内部标识，模型和用户都看不到
    """
    if not expected_pages:
        return False
    if not cited_pages:
        return False
    return bool(set(cited_pages) & set(expected_pages))


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #

def _percentile(values: list[int], p: float) -> float:
    """分位数。样本少时用线性插值，和 numpy 的默认行为一致。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def aggregate(results: list[ItemResult]) -> dict[str, Any]:
    """把逐条结果聚合成四层指标。"""
    if not results:
        return {}

    total = len(results)
    answerable_items = [r for r in results if r.answerable != "none"]
    refusal_items = [r for r in results if r.answerable == "none"]

    # ---- L1 检索 ----
    # 只对"可答题"算召回，拒答题本来就没有正确 chunk
    recall_items = [r for r in answerable_items if r.retrieved_chunks or r.error == ""]
    recall_at_5 = (
        sum(1 for r in recall_items if r.recall_hit) / len(recall_items)
        if recall_items else 0.0
    )
    mrr = (
        sum(r.reciprocal_rank for r in recall_items) / len(recall_items)
        if recall_items else 0.0
    )

    # ---- L2 生成 ----
    citation_ok = (
        sum(1 for r in answerable_items if r.citation_ok) / len(answerable_items)
        if answerable_items else 0.0
    )
    # 应拒答率：R 系列里明确拒答的比例
    refusal_rate = (
        sum(1 for r in refusal_items if r.refused) / len(refusal_items)
        if refusal_items else 0.0
    )
    # 误拒率 = 可答题里「表达了找不到 **且** 没引用到期望页码」的比例。
    #
    # 为什么必须加 citation_ok 这一条：partial 题的正确答案本来就包含
    # 「手册未规定 X」，full 题也可能主动补充「手册未规定 Y」。
    # 只看关键词会把这类好答案判成拒答（实测 D02/D03/D05 全中招）。
    # 但也不能只看「有没有引用」—— 应拒答题里模型也会引用不相关的 chunk
    # （问年终奖却引「迟到早退」）。两个条件组合才准。
    #
    # 为什么仍限定 full：partial 题的标准答案天然带缺口说明，
    # 把它们算进误拒率会让指标失去意义。
    full_items = [r for r in results if r.answerable == "full"]
    false_refusal_rate = (
        sum(1 for r in full_items if r.refused and not r.citation_ok) / len(full_items)
        if full_items else 0.0
    )
    # 关键点覆盖：只统计"可答题且模型确实作答了"的
    answered_items = [r for r in answerable_items if not r.refused]
    keypoint_coverage = (
        sum(r.keypoint_coverage for r in answered_items) / len(answered_items)
        if answered_items else 0.0
    )
    hallucination_count = sum(1 for r in results if r.hallucinated)

    # ---- L3 轨迹 ----
    action_accuracy = sum(1 for r in results if r.action_correct) / total
    avg_steps = sum(r.steps for r in results) / total

    # ---- L4 系统 ----
    latencies = [r.latency_ms for r in results]
    tokens = [r.tokens for r in results]
    total_tokens = sum(tokens)
    wasted_tokens = sum(r.tokens for r in results if r.stop_reason in WASTED_STOP_REASONS)
    wasted_ratio = wasted_tokens / total_tokens if total_tokens else 0.0

    # ---- 按 stop_reason 拆成本（SPEC 5.6）----
    cost_by_reason: dict[str, dict[str, float]] = {}
    for r in results:
        bucket = cost_by_reason.setdefault(
            r.stop_reason or "unknown", {"count": 0, "tokens": 0, "latency_ms": 0}
        )
        bucket["count"] += 1
        bucket["tokens"] += r.tokens
        bucket["latency_ms"] += r.latency_ms
    for bucket in cost_by_reason.values():
        cnt = bucket["count"]
        bucket["avg_tokens"] = round(bucket["tokens"] / cnt, 1)
        bucket["avg_latency_ms"] = round(bucket["latency_ms"] / cnt, 1)

    return {
        "total": total,
        "answerable": len(answerable_items),
        "refusal": len(refusal_items),
        "L1_recall_at_5": round(recall_at_5, 4),
        "L1_mrr": round(mrr, 4),
        "L2_citation_precision": round(citation_ok, 4),
        "L2_refusal_rate": round(refusal_rate, 4),
        "L2_false_refusal_rate": round(false_refusal_rate, 4),
        "L2_keypoint_coverage": round(keypoint_coverage, 4),
        "L2_hallucination_count": hallucination_count,
        "L2_hallucination_rate": round(hallucination_count / total, 4),
        "L3_action_accuracy": round(action_accuracy, 4),
        "L3_avg_steps": round(avg_steps, 2),
        "L4_latency_p50_ms": round(_percentile(latencies, 0.50), 1),
        "L4_latency_p95_ms": round(_percentile(latencies, 0.95), 1),
        "L4_latency_max_ms": max(latencies) if latencies else 0,
        "L4_avg_tokens": round(total_tokens / total, 1),
        "L4_total_tokens": total_tokens,
        "L4_wasted_tokens": wasted_tokens,
        "L4_wasted_cost_ratio": round(wasted_ratio, 4),
        "L4_cost_by_stop_reason": cost_by_reason,
    }


def aggregate_by_type(results: list[ItemResult]) -> dict[str, dict[str, Any]]:
    """按 direct / multi_hop / refusal 分组统计。

    这是最能说明问题的一张表：
    直接事实能答多少、多跳掉了多少、拒答准不准，三个数分开看。
    """
    by_type: dict[str, list[ItemResult]] = {}
    for r in results:
        by_type.setdefault(r.item_type, []).append(r)

    out: dict[str, dict[str, Any]] = {}
    for tkey, group in by_type.items():
        answerable = [r for r in group if r.answerable != "none"]
        out[tkey] = {
            "count": len(group),
            "recall_at_5": round(
                sum(1 for r in answerable if r.recall_hit) / len(answerable), 4
            ) if answerable else None,
            "citation_precision": round(
                sum(1 for r in answerable if r.citation_ok) / len(answerable), 4
            ) if answerable else None,
            "keypoint_coverage": round(
                sum(r.keypoint_coverage for r in answerable) / len(answerable), 4
            ) if answerable else None,
            "action_accuracy": round(
                sum(1 for r in group if r.action_correct) / len(group), 4
            ),
            "avg_steps": round(sum(r.steps for r in group) / len(group), 2),
            "avg_latency_ms": round(sum(r.latency_ms for r in group) / len(group), 1),
        }
    return out


def median(values: list[float]) -> float:
    """给报告用：中位数。"""
    return float(statistics.median(values)) if values else 0.0
