"""The ``seawall eval`` commands."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import typer

# The heavy imports live inside the commands: this module is loaded for every `seawall` invocation
# (to register the group), and evaluating things should cost nothing when you are not.

DEFAULT_TASKS_DIR = Path("evals/tasks")

eval_app = typer.Typer(name="eval", help="Run coding tasks and safety scenarios against the agent")


def _load(tasks_dir: Path, ids: list[str] | None):
    from seawall.evals.task import TaskError, load_tasks

    try:
        return load_tasks(tasks_dir, ids or None)
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(2)


def _progress(trial) -> None:
    mark = "pass" if trial.passed else "FAIL"
    cost = "n/a" if trial.cost_usd is None else f"${trial.cost_usd:.4f}"
    print(
        f"  [{mark}] {trial.task_id} #{trial.trial}  {trial.stop_reason}, {trial.turns} turns, "
        f"{trial.total_tokens:,} tokens, {cost}, {trial.duration_seconds:.1f}s",
        file=sys.stderr,
        flush=True,
    )


@eval_app.command("list")
def eval_list(
    tasks_dir: Path = typer.Option(DEFAULT_TASKS_DIR, "--tasks", help="Directory of task folders"),
) -> None:
    """List the coding tasks."""
    for task in _load(tasks_dir, None):
        print(f"{task.id:<34} {task.difficulty:<7} {', '.join(task.tags):<36} {task.title}")


@eval_app.command("validate")
def eval_validate(
    tasks_dir: Path = typer.Option(DEFAULT_TASKS_DIR, "--tasks", help="Directory of task folders"),
    task: list[str] = typer.Option(None, "--task", help="Only this task id (repeatable)"),
) -> None:
    """Check that every task fails as shipped and passes with its reference solution."""
    from seawall.evals.runner import validate_task

    tasks = _load(tasks_dir, task)

    async def run() -> dict[str, list[str]]:
        scratch_root = Path(tempfile.mkdtemp(prefix="seawall-eval-validate-"))
        try:
            results = await asyncio.gather(*(validate_task(t, scratch_root / t.id) for t in tasks))
            return {t.id: problems for t, problems in zip(tasks, results)}
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

    problems = asyncio.run(run())
    broken = 0
    for task_id, found in problems.items():
        if found:
            broken += 1
            print(f"BROKEN {task_id}")
            for line in found:
                print("   " + line.replace("\n", "\n   "))
        else:
            print(f"ok     {task_id}")
    print(f"{len(problems) - broken} of {len(problems)} tasks are sound")
    if broken:
        raise typer.Exit(1)


@eval_app.command("run")
def eval_run(
    suite: str = typer.Option("coding", "--suite", help="coding or safety"),
    agent: str = typer.Option(
        "oracle",
        "--agent",
        help="Who does the work: oracle, noop, cheater or wrongfix (scripted, free, deterministic), or live (a real model; costs money)",
    ),
    trials: int = typer.Option(1, "--trials", min=1, help="Runs per task, to see variance"),
    jobs: int = typer.Option(4, "--jobs", min=1, help="Trials run in parallel"),
    tasks_dir: Path = typer.Option(DEFAULT_TASKS_DIR, "--tasks", help="Directory of task folders"),
    task: list[str] = typer.Option(None, "--task", help="Only this task or scenario id (repeatable)"),
    out: Path | None = typer.Option(None, "--out", help="Write the results JSON here"),
    work_dir: Path | None = typer.Option(None, "--work-dir", help="Where trial workspaces go (default: .eval-runs/<time>)"),
    model: str | None = typer.Option(None, "--model", help="live: model name (default: the one you configured)"),
    base_url: str | None = typer.Option(None, "--base-url", help="live: API base URL (default: the one you configured)"),
    api_format: str | None = typer.Option(None, "--api-format", help="live: anthropic or openai (default: the one you configured)"),
    permission_mode: str | None = typer.Option(None, "--permission-mode", help="Default: allow only the task's tools"),
    max_budget_usd: float | None = typer.Option(None, "--max-budget-usd", min=0.0001, help="live: spend limit per trial"),
    max_total_tokens: int | None = typer.Option(None, "--max-total-tokens", min=1, help="live: token limit per trial"),
    expect_all_pass: bool = typer.Option(False, "--expect-all-pass", help="Exit 1 unless every trial passes (for CI)"),
    normalize: bool = typer.Option(False, "--normalize", help="Strip times, paths and timings from the results file, so it is fit to commit as a baseline"),
) -> None:
    """Run a suite and print the report."""
    from seawall.evals.report import format_report
    from seawall.evals.runner import AgentSpec, configured_key_env, use_configured_provider
    from seawall.evals.suite import run_safety_suite, run_suite

    if suite not in {"coding", "safety"}:
        print("error: --suite must be coding or safety", file=sys.stderr)
        raise typer.Exit(2)
    spec = AgentSpec(
        kind=agent,
        model=model,
        base_url=base_url,
        api_format=api_format,
        permission_mode=permission_mode,
        max_budget_usd=max_budget_usd,
        max_total_tokens=max_total_tokens,
    )
    if suite == "coding":
        try:
            spec.validate()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise typer.Exit(2)
        if spec.is_live and max_budget_usd is None and max_total_tokens is None:
            print(
                "error: a live run spends real money; give --max-budget-usd or --max-total-tokens "
                "(the limit applies to each trial)",
                file=sys.stderr,
            )
            raise typer.Exit(2)

    run_dir = (work_dir or Path(".eval-runs") / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    if suite == "safety":
        print(f"running the safety scenarios (scripted compromised model) in {run_dir}", file=sys.stderr)
        try:
            result = asyncio.run(run_safety_suite(out_dir=run_dir, jobs=jobs, only=task or None, on_trial=_progress))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise typer.Exit(2)
    else:
        tasks = _load(tasks_dir, task)
        if spec.is_live:
            spec = use_configured_provider(spec)
            os.environ.update(configured_key_env(spec))  # agent_flags forwards it like any key in the environment
        label = f"a live model ({spec.model})" if spec.is_live else f"the {agent} scripted agent"
        print(f"running {len(tasks)} tasks x {trials} with {label} in {run_dir}", file=sys.stderr)
        result = asyncio.run(
            run_suite(tasks, spec, suite="coding", trials=trials, jobs=jobs, out_dir=run_dir, on_trial=_progress)
        )

    destination = out or run_dir / "results.json"
    result.save(destination, normalize=normalize)
    print(format_report(result))
    print(f"\nresults: {destination}")
    failed = any(not t.passed for o in result.tasks.values() for t in o.trials)
    if expect_all_pass and failed:
        raise typer.Exit(1)


@eval_app.command("report")
def eval_report(
    results: Path = typer.Argument(..., help="A results JSON file written by `seawall eval run`"),
    markdown: bool = typer.Option(False, "--markdown", help="Markdown table, for CI summaries"),
) -> None:
    """Print the report for a saved run."""
    from seawall.evals.report import format_report
    from seawall.evals.suite import SuiteResult

    print(format_report(SuiteResult.load(results), markdown=markdown))


@eval_app.command("explain")
def eval_explain(
    results: Path = typer.Argument(..., help="A results JSON file written by `seawall eval run`"),
    task: str | None = typer.Option(None, "--task", help="Only this task"),
    trial: int | None = typer.Option(None, "--trial", min=1, help="Only this trial number"),
) -> None:
    """Say why each failed trial failed: its cause, the evidence, and its trace."""
    from seawall.evals.attribution import explain
    from seawall.evals.suite import SuiteResult

    result = SuiteResult.load(results)
    if task is not None and task not in result.tasks:
        print(f"error: no task {task!r} in {results}", file=sys.stderr)
        raise typer.Exit(2)
    chosen = [
        t
        for task_id, outcome in result.tasks.items()
        if task is None or task_id == task
        for t in outcome.trials
        if (trial is None or t.trial == trial) and (task is not None or trial is not None or not t.passed)
    ]
    if not chosen:
        print("No failed trials." if task is None and trial is None else "No such trial.")
        return
    print("\n\n".join(explain(t) for t in chosen))


@eval_app.command("compare")
def eval_compare(
    baseline: Path = typer.Argument(..., help="Results of the reference run"),
    current: Path = typer.Argument(..., help="Results of the new run"),
    max_pass_drop: float = typer.Option(0.0, "--max-pass-drop", min=0.0, help="Allowed fall of the overall pass rate (0.05 = 5 points)"),
    task_tolerance: float = typer.Option(0.0, "--task-tolerance", min=0.0, help="Allowed fall of one task's pass rate"),
    max_cost_factor: float | None = typer.Option(None, "--max-cost-factor", min=1.0, help="Fail if total cost exceeds the baseline times this"),
    max_token_factor: float | None = typer.Option(None, "--max-token-factor", min=1.0, help="Fail if total tokens exceed the baseline times this"),
) -> None:
    """Compare a run with a baseline; exits 1 on a regression (the CI gate)."""
    from seawall.evals.report import compare
    from seawall.evals.suite import SuiteResult

    outcome = compare(
        SuiteResult.load(baseline),
        SuiteResult.load(current),
        max_pass_drop=max_pass_drop,
        task_tolerance=task_tolerance,
        max_cost_factor=max_cost_factor,
        max_token_factor=max_token_factor,
    )
    print(outcome.format())
    if not outcome.ok:
        raise typer.Exit(1)
