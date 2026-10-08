"""The ``seawall eval`` commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from seawall.cli import app
from seawall.evals.runner import TrialResult
from seawall.evals.suite import SuiteResult, TaskOutcome
from tests.test_evals.conftest import make_task

runner = CliRunner()


def result_file(path: Path, passes: dict[str, bool]) -> Path:
    result = SuiteResult(suite="coding", agent={"kind": "oracle", "model": "scripted-model"}, trials_per_task=1)
    for task_id, passed in passes.items():
        trial = TrialResult(task_id=task_id, trial=1, passed=passed, grader_passed=passed, stop_reason="completed", turns=3, usage={"input_tokens": 90, "output_tokens": 10}, cost_usd=0.01)
        result.tasks[task_id] = TaskOutcome("t", [], [trial])
    result.save(path)
    return path


def test_list(tasks_root: Path) -> None:
    make_task(tasks_root, "alpha", tags=["bugfix", "python"], difficulty="easy")
    make_task(tasks_root, "beta")
    output = runner.invoke(app, ["eval", "list", "--tasks", str(tasks_root)]).output
    assert "alpha" in output and "easy" in output and "bugfix, python" in output and "Fix add" in output
    assert output.index("alpha") < output.index("beta")


def test_validate_passes_for_sound_tasks(tasks_root: Path) -> None:
    make_task(tasks_root, "alpha")
    result = runner.invoke(app, ["eval", "validate", "--tasks", str(tasks_root)])
    assert result.exit_code == 0
    assert "ok     alpha" in result.output and "1 of 1 tasks are sound" in result.output


def test_validate_fails_for_a_broken_task(tasks_root: Path) -> None:
    make_task(tasks_root, "trivial", repo={"calc.py": "def add(a, b):\n    return a + b\n"})
    result = runner.invoke(app, ["eval", "validate", "--tasks", str(tasks_root)])
    assert result.exit_code == 1
    assert "BROKEN trivial" in result.output and "untouched repo" in result.output


def test_a_malformed_task_directory_is_a_usage_error(tasks_root: Path) -> None:
    make_task(tasks_root, "alpha")
    (tasks_root / "alpha" / "task.json").write_text("{nope")
    result = runner.invoke(app, ["eval", "validate", "--tasks", str(tasks_root)])
    assert result.exit_code == 2 and "not valid JSON" in result.output


def test_run_writes_results_and_prints_the_report(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "alpha")
    out = tmp_path / "results.json"
    result = runner.invoke(
        app,
        ["eval", "run", "--tasks", str(tasks_root), "--work-dir", str(tmp_path / "w"), "--out", str(out), "--expect-all-pass"],
    )
    assert result.exit_code == 0, result.output
    assert "pass rate 100.0% over 1 tasks" in result.output
    assert f"results: {out}" in result.output
    saved = SuiteResult.load(out)
    assert saved.tasks["alpha"].trials[0].passed is True


def test_expect_all_pass_turns_failures_into_exit_code_1(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "alpha")
    args = ["eval", "run", "--agent", "noop", "--tasks", str(tasks_root), "--work-dir", str(tmp_path / "w")]
    assert runner.invoke(app, args).exit_code == 0  # a failing run is a result, not an error...
    assert runner.invoke(app, [*args, "--expect-all-pass"]).exit_code == 1  # ...unless CI asks for it


def test_run_rejects_bad_arguments(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "alpha")
    base = ["eval", "run", "--tasks", str(tasks_root), "--work-dir", str(tmp_path / "w")]
    assert runner.invoke(app, [*base, "--suite", "other"]).exit_code == 2
    assert runner.invoke(app, [*base, "--agent", "wizard"]).exit_code == 2
    assert runner.invoke(app, [*base, "--task", "missing"]).exit_code == 2


def test_a_live_run_must_have_a_budget(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "alpha")
    result = runner.invoke(app, ["eval", "run", "--agent", "live", "--tasks", str(tasks_root), "--work-dir", str(tmp_path / "w")])
    assert result.exit_code == 2
    assert "spends real money" in result.output
    assert not (tmp_path / "w" / "alpha").exists()  # nothing was started


def test_the_safety_suite_can_be_run_for_one_scenario(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "run", "--suite", "safety", "--task", "ssh-key-via-shell", "--work-dir", str(tmp_path / "w"), "--expect-all-pass"],
    )
    assert result.exit_code == 0, result.output
    assert "safety suite" in result.output and "ssh-key-via-shell" in result.output
    assert runner.invoke(app, ["eval", "run", "--suite", "safety", "--task", "nope", "--work-dir", str(tmp_path / "w2")]).exit_code == 2


def test_report_prints_a_saved_run(tmp_path: Path) -> None:
    path = result_file(tmp_path / "r.json", {"a": True, "b": False})
    plain = runner.invoke(app, ["eval", "report", str(path)])
    assert plain.exit_code == 0 and "pass rate 50.0%" in plain.output
    assert "### coding suite" in runner.invoke(app, ["eval", "report", str(path), "--markdown"]).output


def test_compare_is_the_regression_gate(tmp_path: Path) -> None:
    base = result_file(tmp_path / "base.json", {"a": True, "b": True})
    same = result_file(tmp_path / "same.json", {"a": True, "b": True})
    worse = result_file(tmp_path / "worse.json", {"a": True, "b": False})

    ok = runner.invoke(app, ["eval", "compare", str(base), str(same)])
    assert ok.exit_code == 0 and "no regressions" in ok.output

    bad = runner.invoke(app, ["eval", "compare", str(base), str(worse)])
    assert bad.exit_code == 1
    assert "REGRESSION: b: pass rate 100% -> 0%" in bad.output

    tolerated = runner.invoke(app, ["eval", "compare", str(base), str(worse), "--task-tolerance", "1", "--max-pass-drop", "0.5"])
    assert tolerated.exit_code == 0


def test_result_files_are_plain_json(tmp_path: Path) -> None:
    data = json.loads(result_file(tmp_path / "r.json", {"a": True}).read_text())
    assert data["schema"] == 1 and data["tasks"]["a"]["trials"][0]["passed"] is True
