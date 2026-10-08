"""Why a trial failed: facts read from its trace, and a cause drawn from them.

*Facts* are observations (how many tool calls ended in an error, in which turn the agent first
wrote a file, whether it ever ran the tests). They are stored with each trial's result. A *cause*
is an interpretation, so it is not stored: :func:`classify` derives it from the stored fields by
fixed rules, which means a better rule can re-read old results, and a baseline file never goes
stale because a rule changed.

The rules take the first that matches. They are deterministic and free, so the same results always
get the same causes, and that is what lets a regression gate rely on them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from seawall.tracing.report import Span, format_session, read_spans, runs, summarize

if TYPE_CHECKING:  # pragma: no cover
    from seawall.evals.runner import TrialResult

EDIT_TOOLS = frozenset({"write_file", "edit_file", "notebook_edit"})
TEST_COMMAND = re.compile(r"\b(pytest|unittest|tox|nosetests)\b")


@dataclass
class TraceFacts:
    """What a trial's trace shows about how the agent went about the task."""

    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0  # tool calls that ran and failed; a refused call is not one
    edits: int = 0  # successful calls of a tool that writes a file
    first_edit_turn: int | None = None
    tests_run: int = 0  # shell calls that run a test runner
    denied_tools: list[str] = field(default_factory=list)
    model_error: str = ""  # the error of the first model call that failed
    time_ms: dict[str, float] = field(default_factory=dict)  # where the run's time went

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TraceFacts:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})


def _attrs(span: Span) -> dict[str, Any]:
    attrs = span.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def facts_from_spans(spans: list[Span]) -> TraceFacts:
    """Count what a run did, from its spans."""
    facts = TraceFacts()
    by_id = {s["span"]: s for s in spans}
    for span in sorted(spans, key=lambda s: s["start"]):
        attrs, status = _attrs(span), span.get("status")
        if span["name"] == "model.call":
            facts.model_calls += 1
            if status == "error" and not facts.model_error:
                facts.model_error = str(attrs.get("error", ""))
        elif span["name"] == "tool.call":
            facts.tool_calls += 1
            tool = str(attrs.get("tool", ""))
            if status == "denied":
                facts.denied_tools.append(tool)
            elif status == "error":
                facts.tool_errors += 1
            if status == "ok" and tool in EDIT_TOOLS:
                facts.edits += 1
                turn = by_id.get(span.get("parent"))
                if facts.first_edit_turn is None and turn is not None and turn["name"] == "turn":
                    facts.first_edit_turn = _attrs(turn).get("index")
            tool_input = attrs.get("input")
            if tool == "bash" and isinstance(tool_input, dict) and TEST_COMMAND.search(str(tool_input.get("command", ""))):
                facts.tests_run += 1
    found = runs(spans)
    summary = summarize(spans, found[-1]["trace"]) if found else None
    if summary is not None:
        facts.time_ms = {
            "total": summary.total_ms,
            "model": summary.model_ms,
            "tools": summary.tools_ms,
            "approval": summary.approval_ms,
            "compaction": summary.compaction_ms,
            "other": summary.other_ms,
        }
        facts.time_ms = {key: round(value, 1) for key, value in facts.time_ms.items()}
    return facts


def trace_files(home: Path) -> list[Path]:
    """The trace files an agent left under its throwaway HOME."""
    directory = home / ".seawall" / "data" / "traces"
    return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []


def facts_from_home(home: Path) -> TraceFacts | None:
    """The facts of a trial's agent, or None when it left no trace."""
    spans = [span for path in trace_files(home) for span in read_spans(path)]
    return facts_from_spans(spans) if spans else None


# ---------------------------------------------------------------------------
# Causes
# ---------------------------------------------------------------------------

LIMIT_CAUSES = frozenset({"budget_exceeded", "token_limit", "time_limit", "max_turns", "loop_detected", "no_price"})

SUMMARIES = {
    "harness_error": "the harness itself failed, so the agent's work was not judged",
    "timeout": "the agent did not finish within the task's time limit",
    "no_result": "the agent exited without reporting a result",
    "tampering": "the agent changed files it must not touch",
    "budget_exceeded": "the run hit its dollar budget",
    "token_limit": "the run hit its token limit",
    "time_limit": "the run hit its time limit",
    "max_turns": "the run used all its turns without finishing",
    "loop_detected": "the loop guard stopped the agent for repeating itself",
    "no_price": "a dollar budget was set but the model has no price",
    "model_error": "the model or its provider returned an error",
    "blocked": "the agent was refused tools and changed nothing",
    "no_attempt": "the agent finished without changing a file",
    "wrong_fix": "the agent changed files but the hidden tests still fail",
    "unknown": "the trial failed in a way the rules do not cover",
}


@dataclass(frozen=True)
class Verdict:
    cause: str
    summary: str
    evidence: tuple[str, ...] = ()


def classify(trial: TrialResult) -> Verdict | None:
    """Why a trial failed, or None when it passed. The first rule that matches decides."""
    if trial.passed:
        return None
    stop = trial.stop_reason
    if stop in ("harness_error", "timeout", "no_result"):
        cause = stop
    elif trial.tampered_files:
        cause = "tampering"
    elif stop in LIMIT_CAUSES:
        cause = stop
    elif stop == "error":
        cause = "model_error"
    elif trial.files_changed == 0 and trial.tool_calls_denied > 0:
        cause = "blocked"
    elif trial.files_changed == 0:
        cause = "no_attempt"
    elif not trial.grader_passed:
        cause = "wrong_fix"
    else:
        cause = "unknown"
    return Verdict(cause, SUMMARIES[cause], tuple(_evidence(cause, trial, TraceFacts.from_dict(trial.trace))))


def _evidence(cause: str, trial: TrialResult, facts: TraceFacts) -> list[str]:
    lines: list[str] = []
    if trial.error:
        lines.append(trial.error)
    if cause == "tampering":
        lines.append("changed " + ", ".join(trial.tampered_files))
    elif cause in LIMIT_CAUSES:
        if trial.detail:
            lines.append(trial.detail)
        cost = f", ${trial.cost_usd:.4f}" if trial.cost_usd is not None else ""
        lines.append(f"used {trial.turns} turns, {trial.total_tokens:,} tokens{cost}")
    elif cause == "model_error":
        lines.append(facts.model_error or trial.detail or "no error text was recorded")
    elif cause == "blocked":
        tools = ", ".join(sorted(set(facts.denied_tools)))
        lines.append(f"{trial.tool_calls_denied} tool calls refused" + (f" ({tools})" if tools else ""))
    elif cause == "no_attempt":
        calls = facts.tool_calls or trial.tool_calls_allowed
        lines.append(f"finished after {trial.turns} turns and {calls} tool calls without writing a file")
    elif cause == "wrong_fix":
        first = f", the first in turn {facts.first_edit_turn}" if facts.first_edit_turn else ""
        lines.append(f"changed {trial.files_changed} file(s){first}")
        if facts.model_calls:  # there is a trace to say so
            lines.append(f"ran the tests itself ({facts.tests_run}x)" if facts.tests_run else "never ran the tests itself")
        tail = trial.grader_output_tail.strip().splitlines()
        if tail:
            lines.append(f"grader: {tail[-1]}")
    if facts.tool_errors:
        lines.append(f"{facts.tool_errors} tool call(s) ended in an error")
    return lines


def cause_counts(trials: Iterable[TrialResult]) -> Counter[str]:
    """How many failed trials there are of each cause."""
    return Counter(verdict.cause for trial in trials if (verdict := classify(trial)) is not None)


def explain(trial: TrialResult) -> str:
    """A trial's cause and evidence, followed by its trace tree when the trace file is still there."""
    head = f"{trial.task_id}  trial {trial.trial}  "
    verdict = classify(trial)
    if verdict is None:
        return head + "passed"
    lines = [f"{head}FAILED  cause: {verdict.cause}", f"  {verdict.summary}"]
    lines += [f"  - {item}" for item in verdict.evidence]
    if trial.run_dir:
        for path in trace_files(Path(trial.run_dir) / "home"):
            lines += ["", *("  " + line for line in format_session(path).splitlines())]
    return "\n".join(lines)
