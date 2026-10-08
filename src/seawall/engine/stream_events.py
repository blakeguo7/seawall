"""Events yielded by the query engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from seawall.api.usage import UsageSnapshot
from seawall.engine.messages import ConversationMessage


@dataclass(frozen=True)
class AssistantTextDelta:
    """Incremental assistant text."""

    text: str


@dataclass(frozen=True)
class AssistantTurnComplete:
    """Completed assistant turn."""

    message: ConversationMessage
    usage: UsageSnapshot


@dataclass(frozen=True)
class ToolExecutionStarted:
    """The engine is about to execute a tool."""

    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionCompleted:
    """A tool has finished executing."""

    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """An error that should be surfaced to the user."""

    message: str
    recoverable: bool = True


@dataclass(frozen=True)
class StatusEvent:
    """A transient system status message shown to the user."""

    message: str


@dataclass(frozen=True)
class CompactProgressEvent:
    """Structured progress event for conversation compaction."""

    phase: Literal[
        "hooks_start",
        "context_collapse_start",
        "context_collapse_end",
        "session_memory_start",
        "session_memory_end",
        "compact_start",
        "compact_retry",
        "compact_end",
        "compact_failed",
    ]
    trigger: Literal["auto", "manual", "reactive"]
    message: str | None = None
    attempt: int | None = None
    checkpoint: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunFinished:
    """The last event of every run: why it ended and what the session has used so far.

    ``stop_reason`` is ``completed`` when the model finished on its own; anything else
    (``max_turns``, ``budget_exceeded``, ``token_limit``, ``time_limit``, ``loop_detected``,
    ``no_price``, ``error``) means the run was cut short and ``detail`` says why.
    ``usage`` and ``cost_usd`` are totals for the session. ``cost_usd`` covers only models with
    a known price; ``unpriced_models`` lists the ones that are missing from it.
    """

    stop_reason: str
    turns: int
    detail: str = ""
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    cost_usd: float | None = None
    unpriced_models: tuple[str, ...] = ()
    duration_seconds: float = 0.0


StreamEvent = (
    AssistantTextDelta
    | AssistantTurnComplete
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | ErrorEvent
    | StatusEvent
    | CompactProgressEvent
    | RunFinished
)
