"""Running many tasks and trials, and the result file they produce."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from seawall.evals.runner import AgentSpec, TrialResult, run_trial
from seawall.evals.scripted import ScriptedServer
from seawall.evals.task import Task

SCHEMA_VERSION = 1


@dataclass
class TaskOutcome:
    title: str
    tags: list[str]
    trials: list[TrialResult] = field(default_factory=list)


@dataclass
class SuiteResult:
    """Everything one ``seawall eval run`` measured; what ``report`` and ``compare`` read."""

    suite: str
    agent: dict[str, Any]
    trials_per_task: int
    tasks: dict[str, TaskOutcome] = field(default_factory=dict)
    created: str = ""
    harness_version: str = ""
    git_commit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "suite": self.suite,
            "created": self.created,
            "harness_version": self.harness_version,
            "git_commit": self.git_commit,
            "agent": self.agent,
            "trials_per_task": self.trials_per_task,
            "tasks": {
                task_id: {"title": o.title, "tags": o.tags, "trials": [t.to_dict() for t in o.trials]}
                for task_id, o in self.tasks.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SuiteResult:
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported result schema {data.get('schema')!r}; expected {SCHEMA_VERSION}")
        result = cls(
            suite=data["suite"],
            agent=data["agent"],
            trials_per_task=data["trials_per_task"],
            created=data.get("created", ""),
            harness_version=data.get("harness_version", ""),
            git_commit=data.get("git_commit", ""),
        )
        for task_id, raw in data["tasks"].items():
            outcome = TaskOutcome(title=raw["title"], tags=list(raw.get("tags", [])))
            outcome.trials = [TrialResult(**trial) for trial in raw["trials"]]
            result.tasks[task_id] = outcome
        return result

    def normalized(self) -> SuiteResult:
        """A copy without what changes from run to run (time, commit, paths, timings), so a
        baseline checked into the repository only changes when the results do."""
        copy = SuiteResult.from_dict(self.to_dict())
        copy.created = copy.git_commit = ""
        for outcome in copy.tasks.values():
            for trial in outcome.trials:
                trial.run_dir = ""
                trial.duration_seconds = trial.grade_seconds = 0.0
        return copy

    def save(self, path: Path, *, normalize: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (self.normalized() if normalize else self).to_dict()
        path.write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> SuiteResult:
        return cls.from_dict(json.loads(Path(path).read_text()))


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


async def run_suite(
    tasks: list[Task],
    agent: AgentSpec,
    *,
    suite: str = "coding",
    trials: int = 1,
    jobs: int = 4,
    out_dir: Path,
    on_trial: Callable[[TrialResult], None] | None = None,
) -> SuiteResult:
    """Run every task ``trials`` times, ``jobs`` at a time, and collect the results."""
    from seawall.cli import __version__

    agent.validate()
    result = SuiteResult(
        suite=suite,
        agent=agent.describe(),
        trials_per_task=trials,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        harness_version=__version__,
        git_commit=_git_commit(),
    )
    for task in tasks:
        result.tasks[task.id] = TaskOutcome(title=task.title, tags=list(task.tags))

    semaphore = asyncio.Semaphore(max(1, jobs))
    server = None if agent.is_live else ScriptedServer().start()

    async def one(task: Task, trial: int) -> TrialResult:
        async with semaphore:
            try:
                outcome = await run_trial(task, trial, agent, out_dir=out_dir, server=server)
            except Exception as exc:  # an infrastructure failure must not hide the other trials
                outcome = TrialResult(
                    task_id=task.id,
                    trial=trial,
                    passed=False,
                    grader_passed=False,
                    stop_reason="harness_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if on_trial is not None:
                on_trial(outcome)
            return outcome

    try:
        outcomes = await asyncio.gather(*(one(task, n) for task in tasks for n in range(1, trials + 1)))
    finally:
        if server is not None:
            server.stop()
    for outcome in outcomes:
        result.tasks[outcome.task_id].trials.append(outcome)
    for task_outcome in result.tasks.values():
        task_outcome.trials.sort(key=lambda t: t.trial)
    return result


async def run_safety_suite(
    *,
    out_dir: Path,
    jobs: int = 4,
    only: list[str] | None = None,
    on_trial: Callable[[TrialResult], None] | None = None,
) -> SuiteResult:
    """Run the safety scenarios and return them in the same shape as a coding suite.

    A scenario "passes" when every expectation about the evidence holds, so the report,
    ``compare`` and the CI gate work on it unchanged.
    """
    from seawall.cli import __version__
    from seawall.evals.safety import build_scenarios, run_scenario

    server = ScriptedServer().start()
    try:
        scenarios = build_scenarios(server)
        if only:
            unknown = sorted(set(only) - {s.id for s in scenarios})
            if unknown:
                raise ValueError(f"unknown scenario(s): {', '.join(unknown)}")
            scenarios = [s for s in scenarios if s.id in set(only)]
        result = SuiteResult(
            suite="safety",
            agent={"kind": "scripted-compromised-model", "model": "scripted-model"},
            trials_per_task=1,
            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            harness_version=__version__,
            git_commit=_git_commit(),
        )
        semaphore = asyncio.Semaphore(max(1, jobs))

        async def one(scenario) -> TrialResult:
            async with semaphore:
                try:
                    outcome = await run_scenario(scenario, out_dir=out_dir, server=server)
                except Exception as exc:
                    outcome = TrialResult(
                        task_id=scenario.id,
                        trial=1,
                        passed=False,
                        grader_passed=False,
                        stop_reason="harness_error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if on_trial is not None:
                    on_trial(outcome)
                return outcome

        outcomes = await asyncio.gather(*(one(s) for s in scenarios))
        for scenario, outcome in zip(scenarios, outcomes):
            tags = ["documents"] if scenario.documents else ["defence"]
            result.tasks[scenario.id] = TaskOutcome(title=scenario.description, tags=tags, trials=[outcome])
        return result
    finally:
        server.stop()
