"""Aggregation, the report, and the regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seawall.evals.report import compare, format_report, summarize, task_stats
from seawall.evals.runner import TrialResult
from seawall.evals.suite import SuiteResult, TaskOutcome


def trial(task_id: str, n: int, passed: bool, *, turns=3, tokens=1000, cost: float | None = 0.01, reason="completed", **extra):
    return TrialResult(
        task_id=task_id,
        trial=n,
        passed=passed,
        grader_passed=passed,
        stop_reason=reason,
        turns=turns,
        usage={"input_tokens": tokens - 100, "output_tokens": 100},
        cost_usd=cost,
        duration_seconds=2.0,
        **extra,
    )


def suite(outcomes: dict[str, list[TrialResult]], *, name="coding") -> SuiteResult:
    result = SuiteResult(suite=name, agent={"kind": "oracle", "model": "scripted-model"}, trials_per_task=max(len(t) for t in outcomes.values()))
    for task_id, trials in outcomes.items():
        result.tasks[task_id] = TaskOutcome(title=f"title of {task_id}", tags=["t"], trials=trials)
    return result


def test_task_stats() -> None:
    outcome = TaskOutcome("t", [], [trial("a", 1, True, turns=2), trial("a", 2, False, turns=6, reason="loop_detected"), trial("a", 3, True, turns=4)])
    stats = task_stats("a", outcome)
    assert (stats.trials, stats.passes) == (3, 2)
    assert stats.pass_rate == pytest.approx(2 / 3)
    assert stats.mean_turns == 4 and stats.mean_tokens == 1000
    assert stats.mean_cost == pytest.approx(0.01)
    assert stats.stop_reasons == {"completed": 2, "loop_detected": 1}


def test_cost_is_unknown_when_any_trial_is_unpriced() -> None:
    outcome = TaskOutcome("t", [], [trial("a", 1, True), trial("a", 2, True, cost=None)])
    assert task_stats("a", outcome).mean_cost is None


def test_summary() -> None:
    result = suite({"a": [trial("a", 1, True), trial("a", 2, True)], "b": [trial("b", 1, True), trial("b", 2, False)], "c": [trial("c", 1, False), trial("c", 2, False)]})
    summary = summarize(result)
    assert summary.tasks == 3 and summary.trials == 6
    assert summary.mean_pass_rate == pytest.approx((1 + 0.5 + 0) / 3)
    assert (summary.tasks_always_passing, summary.tasks_ever_passing) == (1, 2)
    assert summary.total_tokens == 6000 and summary.total_cost == pytest.approx(0.06)


def test_report_table_and_failures() -> None:
    result = suite({"a": [trial("a", 1, True)], "b": [trial("b", 1, False, grader_output_tail="x\nE   assert 1 == 2\n")]})
    text = format_report(result)
    assert "coding suite: agent=oracle model=scripted-model trials/task=1" in text
    assert "a     1/1" in text.replace("  ", " ") or "1/1" in text
    assert "pass rate 50.0% over 2 tasks" in text
    assert "FAILED b trial 1 [completed]: E   assert 1 == 2" in text


def test_report_names_tampering_and_errors() -> None:
    result = suite(
        {
            "a": [trial("a", 1, False, tampered_files=["tests/test_a.py"])],
            "b": [trial("b", 1, False, error="agent did not finish within 5s", reason="timeout")],
        }
    )
    text = format_report(result)
    assert "FAILED a trial 1 [completed]: tampered with tests/test_a.py" in text
    assert "FAILED b trial 1 [timeout]: agent did not finish within 5s" in text


def test_markdown_report() -> None:
    text = format_report(suite({"a": [trial("a", 1, True)]}), markdown=True)
    assert text.startswith("### coding suite")
    assert "| task | pass | turns | tokens | cost | time | stopped by |" in text
    assert "| a | 1/1 |" in text


def test_results_round_trip_through_json(tmp_path: Path) -> None:
    original = suite({"a": [trial("a", 1, True, run_dir="/x")]})
    original.git_commit = "abc123"
    path = tmp_path / "out" / "results.json"
    original.save(path)

    loaded = SuiteResult.load(path)
    assert loaded.to_dict() == original.to_dict()
    assert loaded.tasks["a"].trials[0].run_dir == "/x"

    data = json.loads(path.read_text())
    data["schema"] = 99
    with pytest.raises(ValueError, match="schema"):
        SuiteResult.from_dict(data)


# --- comparing -------------------------------------------------------------------------------


def base_run():
    return suite({"a": [trial("a", 1, True)], "b": [trial("b", 1, True)], "c": [trial("c", 1, False)]})


def test_identical_runs_have_no_regressions() -> None:
    outcome = compare(base_run(), base_run())
    assert outcome.ok and outcome.regressions == [] and "no regressions" in outcome.format()


def test_a_task_that_stopped_passing_is_a_regression() -> None:
    new = suite({"a": [trial("a", 1, True)], "b": [trial("b", 1, False)], "c": [trial("c", 1, False)]})
    outcome = compare(base_run(), new)
    assert not outcome.ok
    assert "b: pass rate 100% -> 0%" in outcome.regressions
    assert any("overall pass rate" in r for r in outcome.regressions)
    assert "REGRESSION: b:" in outcome.format()


def test_a_task_missing_from_the_new_run_is_a_regression() -> None:
    new = suite({"a": [trial("a", 1, True)], "b": [trial("b", 1, True)]})
    assert "c: in the baseline but not in this run" in compare(base_run(), new).regressions


def test_improvements_and_new_tasks_are_reported_but_fine() -> None:
    new = suite({"a": [trial("a", 1, True)], "b": [trial("b", 1, True)], "c": [trial("c", 1, True)], "d": [trial("d", 1, True)]})
    outcome = compare(base_run(), new)
    assert outcome.ok
    assert "c: pass rate 0% -> 100%" in outcome.improvements
    assert any(note.startswith("d: new task") for note in outcome.notes)


def test_tolerances() -> None:
    base = suite({"a": [trial("a", n, n <= 4) for n in range(1, 5)] + [trial("a", 5, True)]})  # 100%
    new = suite({"a": [trial("a", n, n <= 4) for n in range(1, 5)] + [trial("a", 5, False)]})  # 80%
    assert not compare(base, new).ok
    assert compare(base, new, task_tolerance=0.25, max_pass_drop=0.25).ok
    assert not compare(base, new, task_tolerance=0.25, max_pass_drop=0.1).ok


def test_cost_and_token_factors() -> None:
    cheap = suite({"a": [trial("a", 1, True, cost=0.10, tokens=1000)]})
    dear = suite({"a": [trial("a", 1, True, cost=0.30, tokens=3500)]})
    assert compare(cheap, dear).ok  # not checked unless asked
    assert any("total cost" in r for r in compare(cheap, dear, max_cost_factor=2.0).regressions)
    assert any("total tokens" in r for r in compare(cheap, dear, max_token_factor=3.0).regressions)
    assert compare(cheap, dear, max_cost_factor=3.5, max_token_factor=4.0).ok


# --- baselines ---------------------------------------------------------------------------------------


def test_a_normalized_copy_drops_what_changes_between_runs(tmp_path: Path) -> None:
    original = suite({"a": [trial("a", 1, True, run_dir="/tmp/xyz", grade_seconds=1.5)]})
    original.created, original.git_commit = "2026-10-07T00:00:00+00:00", "abc1234"

    clean = original.normalized()
    assert (clean.created, clean.git_commit) == ("", "")
    only = clean.tasks["a"].trials[0]
    assert (only.run_dir, only.duration_seconds, only.grade_seconds) == ("", 0.0, 0.0)
    assert (only.passed, only.turns, only.usage) == (True, 3, {"input_tokens": 900, "output_tokens": 100})
    # the original is untouched, and saving can normalize on the way out
    assert original.git_commit == "abc1234" and original.tasks["a"].trials[0].run_dir == "/tmp/xyz"
    path = tmp_path / "baseline.json"
    original.save(path, normalize=True)
    assert json.loads(path.read_text())["git_commit"] == ""


def test_the_committed_baselines_are_valid_and_all_passing() -> None:
    from tests.test_evals.conftest import REPO_TASKS

    baselines = REPO_TASKS.parent / "baselines"
    for name, suite_name in (("scripted-oracle.json", "coding"), ("safety.json", "safety")):
        result = SuiteResult.load(baselines / name)
        assert result.suite == suite_name
        assert summarize(result).mean_pass_rate == 1.0
        assert all(t.run_dir == "" for o in result.tasks.values() for t in o.trials)  # normalized
