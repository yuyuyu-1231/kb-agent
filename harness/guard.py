"""harness/guard.py — 失控防护（三重熔断）。

Agent 循环最大的工程风险不是「答错」，是「停不下来」：
  - 模型陷入「检索 -> 不满意 -> 重检索」的循环
  - 模型反复调同一个工具、同一组参数，期待不同结果
  - 上下文越滚越大，token 成本失控

三重熔断分别覆盖这三种情况：
  1. max_steps        —— 硬上限，轮数到了就停
  2. token_budget     —— 软上限，预算花完就停
  3. repeat_detection —— 动作指纹，连续两次完全相同的调用判定卡死

设计原则：熔断不抛异常，返回 GuardDecision。
调用方拿到 allowed=False 后走降级路径（通常是转工单），
而不是让整个请求 500。这和 tools.py 的失败处理风格一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class StopReason(str, Enum):
    """终止原因。

    继承 str 是为了能直接 JSON 序列化进 trace 和 API 响应
    （json.dumps 对 str 子类直接输出值，对普通 Enum 会报错）。

    评估阶段要按它统计分布和成本，见 SPEC 5.6。
    """
    ANSWERED = "answered"
    TICKET_CREATED = "ticket_created"
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LOOP_DETECTED = "loop_detected"
    LLM_ERROR = "llm_error"


@dataclass(frozen=True)
class GuardDecision:
    """熔断检查结果。allowed=False 时 reason 必有值。"""
    allowed: bool
    reason: StopReason | None = None
    detail: str = ""


def make_action_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """动作指纹 = sha256(工具名 + 排序后的参数 JSON)。

    三个细节：
    - **排序参数键**：同一组参数，JSON 键顺序不同会得到不同字符串。
      不排序的话 {"a":1,"b":2} 和 {"b":2,"a":1} 会被当成两个动作，漏检。
    - **用哈希而不是存原文**：参数里可能带整页文档内容，存原文占内存。
    - **截断到 16 位十六进制**（64 bit）：碰撞概率 2^-64，实际项目可忽略，
      同时让 trace 里的指纹可读。
    """
    payload = tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Guard:
    """一次 Agent run 对应一个 Guard 实例，用完即弃或 reset 复用。"""

    def __init__(
        self,
        max_steps: int = 6,
        token_budget: int = 30000,
        repeat_threshold: int = 2,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须为正数")
        if token_budget <= 0:
            raise ValueError("token_budget 必须为正数")
        if repeat_threshold < 2:
            raise ValueError("repeat_threshold 至少为 2（第 1 次允许，第 2 次熔断）")

        self.max_steps = max_steps
        self.token_budget = token_budget
        self.repeat_threshold = repeat_threshold
        self.reset()

    # ---------------------------------------------------------------- 状态

    def reset(self) -> None:
        """重置为一次新的 run。"""
        self._steps = 0
        self._last_fingerprint: str | None = None
        self._repeat_count = 0          # 当前指纹已连续出现的额外次数
        self._trips: list[GuardDecision] = []

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def trips(self) -> list[GuardDecision]:
        """本次 run 内触发过的熔断记录，供 trace 落盘。"""
        return list(self._trips)

    # ---------------------------------------------------------------- 检查

    def check_step(self) -> GuardDecision:
        """熔断 1：轮数上限。"""
        if self._steps >= self.max_steps:
            return self._trip(GuardDecision(
                allowed=False,
                reason=StopReason.MAX_STEPS,
                detail=f"已达最大轮数 {self.max_steps}",
            ))
        return GuardDecision(allowed=True)

    def check_budget(self, used_tokens: int) -> GuardDecision:
        """熔断 2：token 预算。"""
        if used_tokens >= self.token_budget:
            return self._trip(GuardDecision(
                allowed=False,
                reason=StopReason.BUDGET_EXHAUSTED,
                detail=f"token 预算耗尽 {used_tokens}/{self.token_budget}",
            ))
        return GuardDecision(allowed=True)

    def check_action(self, tool: str, args: dict[str, Any]) -> GuardDecision:
        """熔断 3：重复动作。

        repeat_threshold=2 时的语义：
          第 1 次调用某工具某参数 -> 允许（记下指纹）
          第 2 次完全相同的调用   -> 熔断

        为什么不允许「重试一次」：工具是确定性的，同样的输入必然得到同样的
        输出。模型重复调用只可能是因为它没读到上一次的 observation，
        或者陷入了错误的推理循环。两种情况都该中断，而不是让它继续烧钱。
        """
        fp = make_action_fingerprint(tool, args)

        if fp == self._last_fingerprint:
            self._repeat_count += 1
            if self._repeat_count >= self.repeat_threshold - 1:
                return self._trip(GuardDecision(
                    allowed=False,
                    reason=StopReason.LOOP_DETECTED,
                    detail=(
                        f"连续 {self._repeat_count + 1} 次调用 {tool} "
                        f"且参数完全相同（指纹 {fp}）"
                    ),
                ))
        else:
            self._last_fingerprint = fp
            self._repeat_count = 0

        return GuardDecision(allowed=True)

    def check(self, used_tokens: int = 0) -> GuardDecision:
        """每轮开始前调用，一次性检查轮数与预算。"""
        decision = self.check_step()
        if not decision.allowed:
            return decision
        return self.check_budget(used_tokens)

    # ---------------------------------------------------------------- 记账

    def begin_step(self) -> None:
        """进入新一轮时调用。"""
        self._steps += 1

    def _trip(self, decision: GuardDecision) -> GuardDecision:
        self._trips.append(decision)
        return decision
