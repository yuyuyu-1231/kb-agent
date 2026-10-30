"""harness/guard.py 的单元测试。全 mock，无外部依赖。"""

from __future__ import annotations

import pytest

from harness.guard import Guard, GuardDecision, StopReason, make_action_fingerprint


# --------------------------------------------------------------------------- #
# 构造校验
# --------------------------------------------------------------------------- #

def test_invalid_construction() -> None:
    with pytest.raises(ValueError):
        Guard(max_steps=0)
    with pytest.raises(ValueError):
        Guard(token_budget=0)
    with pytest.raises(ValueError):
        Guard(repeat_threshold=1)


# --------------------------------------------------------------------------- #
# 熔断 1：轮数
# --------------------------------------------------------------------------- #

def test_max_steps_trips() -> None:
    guard = Guard(max_steps=3)
    for _ in range(3):
        assert guard.check_step().allowed is True
        guard.begin_step()

    decision = guard.check_step()
    assert decision.allowed is False
    assert decision.reason is StopReason.MAX_STEPS
    assert "最大轮数 3" in decision.detail


def test_steps_counter() -> None:
    guard = Guard(max_steps=5)
    guard.begin_step()
    guard.begin_step()
    assert guard.steps == 2


# --------------------------------------------------------------------------- #
# 熔断 2：预算
# --------------------------------------------------------------------------- #

def test_budget_trips() -> None:
    guard = Guard(token_budget=1000)
    assert guard.check_budget(999).allowed is True

    decision = guard.check_budget(1000)
    assert decision.allowed is False
    assert decision.reason is StopReason.BUDGET_EXHAUSTED


# --------------------------------------------------------------------------- #
# 熔断 3：重复动作
# --------------------------------------------------------------------------- #

def test_repeat_action_trips_on_second_call() -> None:
    """连续两次同工具同参数 -> 第二次熔断。"""
    guard = Guard(repeat_threshold=2)
    args = {"query": "年假"}

    assert guard.check_action("search_knowledge", args).allowed is True

    decision = guard.check_action("search_knowledge", args)
    assert decision.allowed is False
    assert decision.reason is StopReason.LOOP_DETECTED
    assert "search_knowledge" in decision.detail


def test_different_args_do_not_trip() -> None:
    guard = Guard(repeat_threshold=2)
    assert guard.check_action("search_knowledge", {"query": "年假"}).allowed is True
    assert guard.check_action("search_knowledge", {"query": "病假"}).allowed is True


def test_different_tool_does_not_trip() -> None:
    guard = Guard(repeat_threshold=2)
    assert guard.check_action("search_knowledge", {"q": "x"}).allowed is True
    assert guard.check_action("read_document_page", {"q": "x"}).allowed is True


def test_arg_key_order_irrelevant() -> None:
    """参数键顺序不同必须视为同一动作（否则会漏检）。"""
    fp1 = make_action_fingerprint("f", {"a": 1, "b": 2})
    fp2 = make_action_fingerprint("f", {"b": 2, "a": 1})
    assert fp1 == fp2


def test_non_repeat_after_different_action_resets() -> None:
    """A -> B -> A 不算连续重复，不应熔断。"""
    guard = Guard(repeat_threshold=2)
    assert guard.check_action("t", {"q": "a"}).allowed is True
    assert guard.check_action("t", {"q": "b"}).allowed is True
    assert guard.check_action("t", {"q": "a"}).allowed is True


def test_threshold_three() -> None:
    """repeat_threshold=3 时，第 3 次相同调用才熔断。"""
    guard = Guard(repeat_threshold=3)
    args = {"q": "same"}
    assert guard.check_action("t", args).allowed is True
    assert guard.check_action("t", args).allowed is True
    assert guard.check_action("t", args).allowed is False


# --------------------------------------------------------------------------- #
# 联合检查与状态
# --------------------------------------------------------------------------- #

def test_check_combines_step_and_budget() -> None:
    guard = Guard(max_steps=2, token_budget=100)
    assert guard.check(used_tokens=50).allowed is True

    decision = guard.check(used_tokens=100)
    assert decision.allowed is False
    assert decision.reason is StopReason.BUDGET_EXHAUSTED


def test_trips_recorded() -> None:
    guard = Guard(max_steps=1, token_budget=10)
    guard.begin_step()
    guard.check_step()
    guard.check_budget(10)
    assert len(guard.trips) == 2
    assert all(isinstance(t, GuardDecision) for t in guard.trips)


def test_reset_clears_state() -> None:
    guard = Guard(max_steps=1)
    guard.begin_step()
    guard.check_action("t", {"a": 1})
    guard.check_step()
    assert guard.steps == 1
    assert len(guard.trips) == 1

    guard.reset()
    assert guard.steps == 0
    assert guard.trips == []
    # 指纹也要清空，否则新一轮第一次调用会被误判为重复
    assert guard.check_action("t", {"a": 1}).allowed is True


def test_stop_reason_is_json_serializable() -> None:
    """StopReason 继承 str，必须能直接进 JSON（trace / API 响应要用）。"""
    import json

    payload = json.dumps({"reason": StopReason.TICKET_CREATED})
    assert '"ticket_created"' in payload
