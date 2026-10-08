"""Limits that stop a run: spend, tokens, time, and an agent going in circles."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Mapping

from seawall.engine.cost_tracker import CostTracker

if TYPE_CHECKING:  # pragma: no cover
    from seawall.config.settings import LimitSettings


class StopReason(str, Enum):
    """Why a run ended. Anything other than ``COMPLETED`` means it was cut short."""

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    BUDGET = "budget_exceeded"
    TOKEN_LIMIT = "token_limit"
    TIME_LIMIT = "time_limit"
    LOOP = "loop_detected"
    NO_PRICE = "no_price"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class LimitHit:
    reason: StopReason
    detail: str


@dataclass(frozen=True)
class LoopGuardConfig:
    """When a repeating agent is warned and when it is stopped.

    The same call with the same result, seen ``warn_after`` times within the last
    ``window`` calls, earns a warning appended to the tool result; ``stop_after``
    ends the run. Consecutive failing calls are handled the same way.
    """

    enabled: bool = True
    warn_after: int = 3
    stop_after: int = 5
    window: int = 12
    warn_errors_after: int = 4
    stop_errors_after: int = 8


@dataclass(frozen=True)
class RunLimits:
    """Limits checked before every model call.

    ``max_budget_usd`` and ``max_total_tokens`` count the whole session; ``max_seconds``
    counts one prompt. A call that starts below a limit can finish above it, so the
    overshoot is at most one model call (and one batch of tool calls).
    """

    max_budget_usd: float | None = None
    max_total_tokens: int | None = None
    max_seconds: float | None = None
    loop: LoopGuardConfig = field(default_factory=LoopGuardConfig)

    @classmethod
    def from_settings(cls, settings: LimitSettings) -> RunLimits:
        guard = settings.loop_guard
        return cls(
            max_budget_usd=settings.max_budget_usd,
            max_total_tokens=settings.max_total_tokens,
            max_seconds=settings.max_seconds,
            loop=LoopGuardConfig(
                enabled=guard.enabled,
                warn_after=guard.warn_after,
                stop_after=guard.stop_after,
                window=guard.window,
                warn_errors_after=guard.warn_errors_after,
                stop_errors_after=guard.stop_errors_after,
            ),
        )

    def check(self, cost: CostTracker | None, *, model: str, elapsed: float) -> LimitHit | None:
        if self.max_seconds is not None and elapsed >= self.max_seconds:
            return LimitHit(
                StopReason.TIME_LIMIT,
                f"Time limit reached: {elapsed:.0f}s of {self.max_seconds:g}s.",
            )
        if cost is None:
            return None
        if self.max_total_tokens is not None and cost.total.all_tokens >= self.max_total_tokens:
            return LimitHit(
                StopReason.TOKEN_LIMIT,
                f"Token limit reached: {cost.total.all_tokens} of {self.max_total_tokens} tokens.",
            )
        if self.max_budget_usd is not None:
            if cost.prices.lookup(model) is None:
                return LimitHit(
                    StopReason.NO_PRICE,
                    f"A dollar budget is set but no price is known for {model}, so spend cannot be "
                    "measured. Add the model under 'pricing' in settings.json, or limit tokens "
                    "instead of dollars.",
                )
            if cost.cost_usd >= self.max_budget_usd:
                return LimitHit(
                    StopReason.BUDGET,
                    f"Budget reached: ${cost.cost_usd:.4f} of ${self.max_budget_usd:.2f}.",
                )
        return None


@dataclass(frozen=True)
class LoopVerdict:
    warn: str | None = None
    stop: str | None = None


class LoopGuard:
    """Watches tool calls for an agent that is repeating itself without getting anywhere."""

    def __init__(self, config: LoopGuardConfig) -> None:
        self._config = config
        self._recent: deque[tuple[str, str, str]] = deque(maxlen=max(config.window, 1))
        self._warned: set[tuple[str, str, str]] = set()
        self._error_streak = 0
        self._warned_errors = False

    def observe(
        self, tool_name: str, tool_input: Mapping[str, object], output: str, is_error: bool
    ) -> LoopVerdict:
        """Record one finished tool call and say whether the agent should be warned or stopped."""
        config = self._config
        if not config.enabled:
            return LoopVerdict()
        key = (
            tool_name,
            json.dumps(tool_input, sort_keys=True, default=str),
            hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()[:16],
        )
        self._recent.append(key)
        repeats = self._recent.count(key)
        self._error_streak = self._error_streak + 1 if is_error else 0

        if repeats >= config.stop_after:
            return LoopVerdict(
                stop=(
                    f"Loop detected: {tool_name} ran the same call {repeats} times with the same "
                    "result, so the run was stopped."
                )
            )
        if self._error_streak >= config.stop_errors_after:
            return LoopVerdict(
                stop=(
                    f"Loop detected: {self._error_streak} tool calls in a row failed, so the run "
                    "was stopped."
                )
            )
        if repeats >= config.warn_after and key not in self._warned:
            self._warned.add(key)
            return LoopVerdict(
                warn=(
                    f"[loop guard] You have made this exact {tool_name} call {repeats} times and "
                    "got the same result each time. Repeating it will not change anything. Try a "
                    "different approach, or stop and explain what is blocking you."
                )
            )
        if self._error_streak >= config.warn_errors_after and not self._warned_errors:
            self._warned_errors = True
            return LoopVerdict(
                warn=(
                    f"[loop guard] The last {self._error_streak} tool calls all failed. Look at the "
                    "errors, change your approach, or stop and explain what is blocking you."
                )
            )
        if not is_error:
            self._warned_errors = False
        return LoopVerdict()
