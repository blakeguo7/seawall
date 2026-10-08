"""Turning results into tables, and comparing two runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Sequence

from seawall.evals.attribution import cause_counts
from seawall.evals.runner import TrialResult
from seawall.evals.suite import SuiteResult, TaskOutcome


@dataclass(frozen=True)
class TaskStats:
    task_id: str
    title: str
    trials: int
    passes: int
    mean_turns: float
    mean_tokens: float
    mean_cost: float | None  # None when any trial's model had no price
    mean_duration: float
    stop_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0


@dataclass(frozen=True)
class Summary:
    tasks: int
    trials: int
    mean_pass_rate: float  # averaged over tasks
    tasks_always_passing: int
    tasks_ever_passing: int  # pass@k: at least one trial passed
    total_tokens: int
    total_cost: float | None
    mean_turns: float
    mean_duration: float


def task_stats(task_id: str, outcome: TaskOutcome) -> TaskStats:
    trials: Sequence[TrialResult] = outcome.trials
    reasons: dict[str, int] = {}
    for trial in trials:
        reasons[trial.stop_reason] = reasons.get(trial.stop_reason, 0) + 1
    costs = [t.cost_usd for t in trials]
    return TaskStats(
        task_id=task_id,
        title=outcome.title,
        trials=len(trials),
        passes=sum(1 for t in trials if t.passed),
        mean_turns=mean(t.turns for t in trials) if trials else 0.0,
        mean_tokens=mean(t.total_tokens for t in trials) if trials else 0.0,
        mean_cost=None if any(c is None for c in costs) or not costs else mean(c for c in costs if c is not None),
        mean_duration=mean(t.duration_seconds for t in trials) if trials else 0.0,
        stop_reasons=reasons,
    )


def all_stats(result: SuiteResult) -> list[TaskStats]:
    return [task_stats(task_id, outcome) for task_id, outcome in sorted(result.tasks.items())]


def summarize(result: SuiteResult) -> Summary:
    stats = all_stats(result)
    trials = [t for outcome in result.tasks.values() for t in outcome.trials]
    costs = [t.cost_usd for t in trials]
    return Summary(
        tasks=len(stats),
        trials=len(trials),
        mean_pass_rate=mean(s.pass_rate for s in stats) if stats else 0.0,
        tasks_always_passing=sum(1 for s in stats if s.trials and s.passes == s.trials),
        tasks_ever_passing=sum(1 for s in stats if s.passes > 0),
        total_tokens=sum(t.total_tokens for t in trials),
        total_cost=None if any(c is None for c in costs) or not costs else sum(c for c in costs if c is not None),
        mean_turns=mean(t.turns for t in trials) if trials else 0.0,
        mean_duration=mean(t.duration_seconds for t in trials) if trials else 0.0,
    )


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def format_report(result: SuiteResult, *, markdown: bool = False, show_failures: bool = True) -> str:
    """A table of tasks with pass rate, turns, tokens, cost and time, then the totals."""
    rows = [
        (
            s.task_id,
            f"{s.passes}/{s.trials}",
            f"{s.mean_turns:.1f}",
            f"{s.mean_tokens:,.0f}",
            _money(s.mean_cost),
            f"{s.mean_duration:.1f}s",
            ",".join(f"{k}" for k in sorted(s.stop_reasons)),
        )
        for s in all_stats(result)
    ]
    header = ("task", "pass", "turns", "tokens", "cost", "time", "stopped by")
    summary = summarize(result)
    agent = result.agent
    lines = [
        f"{'### ' if markdown else ''}{result.suite} suite: agent={agent.get('kind')} "
        f"model={agent.get('model') or 'default'} trials/task={result.trials_per_task}"
        + (f" commit={result.git_commit}" if result.git_commit else "")
    ]
    if markdown:
        lines += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
    else:
        widths = [max(len(str(item)) for item in column) for column in zip(header, *rows)]
        lines.append("  ".join(h.ljust(w) for h, w in zip(header, widths)))
        lines += ["  ".join(str(c).ljust(w) for c, w in zip(row, widths)) for row in rows]
    lines += [
        "",
        f"pass rate {summary.mean_pass_rate:.1%} over {summary.tasks} tasks "
        f"({summary.tasks_always_passing} always passed, {summary.tasks_ever_passing} passed at least once)",
        f"{summary.trials} trials, {summary.total_tokens:,} tokens, cost {_money(summary.total_cost)}, "
        f"{summary.mean_turns:.1f} turns and {summary.mean_duration:.1f}s per trial on average",
    ]
    if show_failures:
        failed = [t for o in result.tasks.values() for t in o.trials if not t.passed]
        if failed:
            causes = cause_counts(failed)
            lines.append("failure causes: " + ", ".join(f"{name} x{count}" for name, count in causes.most_common()))
        for trial in failed:
            why = (
                f"tampered with {', '.join(trial.tampered_files)}"
                if trial.tampered_files
                else trial.error or trial.grader_output_tail.strip().splitlines()[-1:] or "grading failed"
            )
            if isinstance(why, list):
                why = why[0] if why else "grading failed"
            lines.append(f"FAILED {trial.task_id} trial {trial.trial} [{trial.stop_reason}]: {why}")
    return "\n".join(lines)


@dataclass
class Comparison:
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.regressions

    def format(self) -> str:
        lines = [f"REGRESSION: {line}" for line in self.regressions]
        lines += [f"improved: {line}" for line in self.improvements]
        lines += [f"note: {line}" for line in self.notes]
        lines.append("no regressions" if self.ok else f"{len(self.regressions)} regression(s)")
        return "\n".join(lines)


def compare(
    base: SuiteResult,
    new: SuiteResult,
    *,
    max_pass_drop: float = 0.0,
    task_tolerance: float = 0.0,
    max_cost_factor: float | None = None,
    max_token_factor: float | None = None,
) -> Comparison:
    """Check a new run against a baseline.

    A regression is a task whose pass rate fell by more than ``task_tolerance``, a baseline task
    that was not run, an overall pass-rate drop beyond ``max_pass_drop``, or (when asked) total
    cost or tokens above the baseline times a factor.
    """
    outcome = Comparison()
    base_stats = {s.task_id: s for s in all_stats(base)}
    new_stats = {s.task_id: s for s in all_stats(new)}
    for task_id, before in base_stats.items():
        after = new_stats.get(task_id)
        if after is None:
            outcome.regressions.append(f"{task_id}: in the baseline but not in this run")
        elif after.pass_rate < before.pass_rate - task_tolerance - 1e-9:
            outcome.regressions.append(f"{task_id}: pass rate {before.pass_rate:.0%} -> {after.pass_rate:.0%}")
        elif after.pass_rate > before.pass_rate + 1e-9:
            outcome.improvements.append(f"{task_id}: pass rate {before.pass_rate:.0%} -> {after.pass_rate:.0%}")
    for task_id in sorted(set(new_stats) - set(base_stats)):
        outcome.notes.append(f"{task_id}: new task, not in the baseline")

    before_summary, after_summary = summarize(base), summarize(new)
    drop = before_summary.mean_pass_rate - after_summary.mean_pass_rate
    if drop > max_pass_drop + 1e-9:
        outcome.regressions.append(
            f"overall pass rate {before_summary.mean_pass_rate:.1%} -> {after_summary.mean_pass_rate:.1%}"
        )
    if max_cost_factor is not None and before_summary.total_cost and after_summary.total_cost is not None:
        if after_summary.total_cost > before_summary.total_cost * max_cost_factor:
            outcome.regressions.append(
                f"total cost {_money(before_summary.total_cost)} -> {_money(after_summary.total_cost)} "
                f"(more than {max_cost_factor:g}x)"
            )
    if max_token_factor is not None and before_summary.total_tokens:
        if after_summary.total_tokens > before_summary.total_tokens * max_token_factor:
            outcome.regressions.append(
                f"total tokens {before_summary.total_tokens:,} -> {after_summary.total_tokens:,} "
                f"(more than {max_token_factor:g}x)"
            )
    return outcome
