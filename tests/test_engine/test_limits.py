"""Run limits and the loop guard."""

from __future__ import annotations

import pytest

from seawall.api.usage import UsageSnapshot
from seawall.config.settings import LimitSettings, LoopGuardSettings
from seawall.engine.cost_tracker import CostTracker
from seawall.engine.limits import LoopGuard, LoopGuardConfig, RunLimits, StopReason
from seawall.engine.pricing import ModelPrice, PriceTable


def tracker_with(cost_usd: float = 0.0, tokens: int = 0, *, model: str = "priced") -> CostTracker:
    tracker = CostTracker(PriceTable({"priced": ModelPrice(1, 1)}))
    if cost_usd:
        tracker.add(UsageSnapshot(input_tokens=int(cost_usd * 1_000_000)), model=model)
    if tokens:
        tracker.add(UsageSnapshot(input_tokens=tokens), model=model)
    return tracker


# --- RunLimits ---------------------------------------------------------------------------


def test_no_limits_never_stops() -> None:
    assert RunLimits().check(tracker_with(100), model="priced", elapsed=1e9) is None


def test_budget_stops_once_it_is_reached() -> None:
    limits = RunLimits(max_budget_usd=1.0)
    assert limits.check(tracker_with(0.99), model="priced", elapsed=0) is None
    hit = limits.check(tracker_with(1.0), model="priced", elapsed=0)
    assert hit is not None and hit.reason is StopReason.BUDGET
    assert "$1.0000 of $1.00" in hit.detail


def test_a_dollar_budget_needs_a_price_for_the_model_in_use() -> None:
    hit = RunLimits(max_budget_usd=1.0).check(tracker_with(), model="mystery", elapsed=0)
    assert hit is not None and hit.reason is StopReason.NO_PRICE
    assert "mystery" in hit.detail and "pricing" in hit.detail


def test_token_limit_counts_cache_traffic() -> None:
    tracker = CostTracker()
    tracker.add(UsageSnapshot(input_tokens=10, cache_read_input_tokens=90), model="m")
    assert RunLimits(max_total_tokens=101).check(tracker, model="m", elapsed=0) is None
    hit = RunLimits(max_total_tokens=100).check(tracker, model="m", elapsed=0)
    assert hit is not None and hit.reason is StopReason.TOKEN_LIMIT


def test_token_limit_works_for_unpriced_models() -> None:
    tracker = CostTracker()
    tracker.add(UsageSnapshot(input_tokens=50), model="mystery")
    assert RunLimits(max_total_tokens=50).check(tracker, model="mystery", elapsed=0) is not None


def test_time_limit() -> None:
    limits = RunLimits(max_seconds=10)
    assert limits.check(None, model="m", elapsed=9.9) is None
    hit = limits.check(None, model="m", elapsed=10)
    assert hit is not None and hit.reason is StopReason.TIME_LIMIT


def test_limits_come_from_settings() -> None:
    settings = LimitSettings(
        max_budget_usd=2, max_total_tokens=500, max_seconds=30, loop_guard=LoopGuardSettings(warn_after=2, stop_after=4)
    )
    limits = RunLimits.from_settings(settings)
    assert (limits.max_budget_usd, limits.max_total_tokens, limits.max_seconds) == (2, 500, 30)
    assert (limits.loop.warn_after, limits.loop.stop_after) == (2, 4)


def test_loop_guard_settings_are_validated() -> None:
    with pytest.raises(ValueError):
        LoopGuardSettings(warn_after=5, stop_after=5)
    with pytest.raises(ValueError):
        LoopGuardSettings(warn_errors_after=8, stop_errors_after=8)
    with pytest.raises(ValueError):
        LoopGuardSettings(window=4, stop_after=5)
    with pytest.raises(ValueError):
        LimitSettings(max_budget_usd=0)


# --- loop guard ------------------------------------------------------------------------------


def observe(guard: LoopGuard, name="bash", tool_input=None, output="same", error=False):
    return guard.observe(name, tool_input or {"command": "pytest"}, output, error)


def test_repeating_the_same_call_with_the_same_result_is_warned_once_then_stopped() -> None:
    guard = LoopGuard(LoopGuardConfig())
    verdicts = [observe(guard) for _ in range(5)]

    assert [v.warn is not None for v in verdicts] == [False, False, True, False, False]
    assert [v.stop is not None for v in verdicts] == [False, False, False, False, True]
    assert "exact bash call 3 times" in verdicts[2].warn
    assert "same call 5 times" in verdicts[4].stop


def test_the_same_call_with_a_different_result_is_progress() -> None:
    guard = LoopGuard(LoopGuardConfig())
    for attempt in range(20):
        verdict = observe(guard, output=f"{attempt} tests failed")
        assert verdict.warn is None and verdict.stop is None


def test_different_inputs_are_different_calls() -> None:
    guard = LoopGuard(LoopGuardConfig())
    for n in range(20):
        verdict = observe(guard, tool_input={"command": f"cat file{n}"})
        assert verdict.warn is None and verdict.stop is None


def test_alternating_calls_are_still_caught() -> None:
    guard = LoopGuard(LoopGuardConfig())
    verdicts = []
    for _ in range(5):
        verdicts.append(observe(guard, tool_input={"command": "a"}))
        verdicts.append(observe(guard, tool_input={"command": "b"}))
    assert any(v.stop for v in verdicts)


def test_old_calls_fall_out_of_the_window() -> None:
    guard = LoopGuard(LoopGuardConfig(window=6, warn_after=3, stop_after=5))
    observe(guard)
    observe(guard)
    for n in range(10):  # enough other calls to push the first two out
        observe(guard, tool_input={"command": f"other{n}"}, output=f"o{n}")
    verdict = observe(guard)  # only the third sighting inside the window
    assert verdict.warn is None


def test_a_streak_of_failures_is_warned_then_stopped() -> None:
    guard = LoopGuard(LoopGuardConfig())
    verdicts = [
        observe(guard, tool_input={"command": f"try{n}"}, output=f"error {n}", error=True)
        for n in range(8)
    ]
    assert verdicts[3].warn is not None and "last 4 tool calls all failed" in verdicts[3].warn
    assert all(v.warn is None for v in verdicts[:3] + verdicts[4:])
    assert all(v.stop is None for v in verdicts[:7])
    assert verdicts[7].stop is not None and "8 tool calls in a row failed" in verdicts[7].stop


def test_a_success_ends_the_failure_streak() -> None:
    guard = LoopGuard(LoopGuardConfig())
    for n in range(20):
        failing = n % 3 != 2
        verdict = observe(guard, tool_input={"command": f"c{n}"}, output=f"o{n}", error=failing)
        assert verdict.stop is None


def test_the_guard_can_be_switched_off() -> None:
    guard = LoopGuard(LoopGuardConfig(enabled=False))
    for _ in range(50):
        verdict = observe(guard)
        assert verdict.warn is None and verdict.stop is None
