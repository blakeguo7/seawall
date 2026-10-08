"""Why a trial failed: facts from the trace, causes from fixed rules, and the commands that show them."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import seawall.cli as cli
from seawall.evals.attribution import TraceFacts, cause_counts, classify, explain, facts_from_spans
from seawall.evals.report import format_report
from seawall.evals.runner import AgentSpec
from seawall.evals.suite import run_suite
from seawall.evals.task import load_tasks
from tests.test_evals.conftest import make_task
from tests.test_evals.test_report import suite, trial
from tests.test_tracing.test_report import span

runner = CliRunner()


# --- the rules ------------------------------------------------------------------------------


def test_a_trial_that_passed_has_no_cause() -> None:
    assert classify(trial("a", 1, True)) is None


@pytest.mark.parametrize(
    ("cause", "reason", "extra"),
    [
        ("harness_error", "harness_error", {}),
        ("timeout", "timeout", {}),
        ("no_result", "no_result", {}),
        ("tampering", "completed", {"tampered_files": ["tests/test_a.py"], "files_changed": 1}),
        ("budget_exceeded", "budget_exceeded", {}),
        ("token_limit", "token_limit", {}),
        ("time_limit", "time_limit", {}),
        ("max_turns", "max_turns", {}),
        ("loop_detected", "loop_detected", {}),
        ("no_price", "no_price", {}),
        ("model_error", "error", {}),
        ("blocked", "completed", {"tool_calls_denied": 2}),
        ("no_attempt", "completed", {}),
        ("wrong_fix", "completed", {"files_changed": 1}),
    ],
)
def test_each_failure_gets_the_cause_that_fits_it(cause: str, reason: str, extra: dict) -> None:
    verdict = classify(trial("a", 1, False, reason=reason, **extra))

    assert verdict.cause == cause and verdict.summary


@pytest.mark.parametrize(
    ("expected", "reason", "extra"),
    [
        ("timeout", "timeout", {"tampered_files": ["t"], "files_changed": 1}),  # not judged at all
        ("tampering", "budget_exceeded", {"tampered_files": ["t"], "files_changed": 1}),  # cheating over a limit
        ("max_turns", "max_turns", {"files_changed": 2}),  # a limit over a wrong fix
        ("wrong_fix", "completed", {"files_changed": 1, "tool_calls_denied": 3}),  # refusals matter only if nothing was written
    ],
)
def test_the_first_rule_that_matches_decides(expected: str, reason: str, extra: dict) -> None:
    assert classify(trial("a", 1, False, reason=reason, **extra)).cause == expected


def test_a_wrong_fix_says_what_the_agent_did_before_giving_up() -> None:
    facts = TraceFacts(model_calls=3, tool_calls=3, tool_errors=1, edits=1, first_edit_turn=2, tests_run=1)
    failed = trial("a", 1, False, files_changed=1, trace=facts.to_dict(), grader_output_tail="...\nE   assert 1 == 2\n")

    evidence = classify(failed).evidence

    assert "changed 1 file(s), the first in turn 2" in evidence
    assert "ran the tests itself (1x)" in evidence
    assert "grader: E   assert 1 == 2" in evidence
    assert "1 tool call(s) ended in an error" in evidence


def test_never_having_run_the_tests_is_said_only_when_there_is_a_trace_to_say_it() -> None:
    with_trace = trial("a", 1, False, files_changed=1, trace=TraceFacts(model_calls=2).to_dict())
    without = trial("a", 2, False, files_changed=1)

    assert "never ran the tests itself" in classify(with_trace).evidence
    assert not any("tests itself" in line for line in classify(without).evidence)


def test_evidence_names_what_was_refused_and_what_the_provider_said() -> None:
    blocked = trial("a", 1, False, tool_calls_denied=3, trace=TraceFacts(denied_tools=["bash", "bash", "write_file"]).to_dict())
    broken = trial("a", 2, False, reason="error", trace=TraceFacts(model_error="RuntimeError: 503").to_dict())
    limited = trial("a", 3, False, reason="max_turns", turns=30, tokens=5000, detail="stopped after 30 turns")

    assert "3 tool calls refused (bash, write_file)" in classify(blocked).evidence
    assert "RuntimeError: 503" in classify(broken).evidence
    assert "stopped after 30 turns" in classify(limited).evidence
    assert "used 30 turns, 5,000 tokens, $0.0100" in classify(limited).evidence


def test_old_results_without_a_trace_still_get_a_cause() -> None:
    assert classify(trial("a", 1, False, files_changed=1)).cause == "wrong_fix"  # trace == {}


def test_causes_are_counted_over_failed_trials_only() -> None:
    trials = [trial("a", 1, True), trial("a", 2, False), trial("b", 1, False), trial("b", 2, False, files_changed=1)]

    assert cause_counts(trials) == {"no_attempt": 2, "wrong_fix": 1}


# --- the facts ------------------------------------------------------------------------------


def test_facts_count_what_the_agent_did_turn_by_turn() -> None:
    spans = [
        span("run", 0, 10_000, id="run", stop_reason="completed", turns=2),
        span("turn", 0, 4_000, id="t1", parent="run", index=1),
        span("turn", 4, 6_000, id="t2", parent="run", index=2),
        span("model.call", 0, 1_000, id="m1", parent="t1", model="m"),
        span("model.call", 4, 1_000, id="m2", parent="t2", status="error", model="m", error="RuntimeError: 503"),
        span("tool.call", 1, 500, id="r", parent="t1", tool="read_file"),
        span("tool.call", 1.6, 500, id="d", parent="t1", tool="write_file", status="denied", denied_by="policy"),
        span("tool.call", 5, 500, id="w", parent="t2", tool="write_file"),
        span("tool.call", 6, 500, id="p", parent="t2", tool="bash", input={"command": "python -m pytest -q"}),
        span("tool.call", 7, 500, id="b", parent="t2", tool="bash", status="error", input={"command": "ls missing"}),
    ]

    facts = facts_from_spans(spans)

    assert (facts.model_calls, facts.tool_calls, facts.tool_errors) == (2, 5, 1)
    assert (facts.edits, facts.first_edit_turn, facts.tests_run) == (1, 2, 1)  # the denied write is not an edit
    assert facts.denied_tools == ["write_file"] and facts.model_error == "RuntimeError: 503"
    assert facts.time_ms["total"] == 10_000 and set(facts.time_ms) == {"total", "model", "tools", "approval", "compaction", "other"}


def test_facts_survive_a_round_trip_and_ignore_what_they_do_not_know() -> None:
    facts = TraceFacts(model_calls=2, denied_tools=["bash"], time_ms={"total": 5.0})

    assert TraceFacts.from_dict(facts.to_dict()) == facts
    assert TraceFacts.from_dict({"model_calls": 1, "added_by_a_newer_version": True}) == TraceFacts(model_calls=1)
    assert TraceFacts.from_dict(None) == TraceFacts()


# --- whole trials against scripted agents of known kinds ------------------------------------


@pytest.fixture
def alpha(tasks_root: Path):
    make_task(
        tasks_root,
        "alpha",
        protected=["tests/test_calc.py"],
        repo={"calc.py": "def add(a, b):\n    return a - b\n", "tests/test_calc.py": "from calc import add\n\n\ndef test():\n    assert add(1, 1) == 2\n"},
        solution={"calc.py": "def add(a, b):\n    return a + b\n"},
    )
    return load_tasks(tasks_root)


@pytest.mark.parametrize(
    ("agent", "cause"), [("noop", "no_attempt"), ("cheater", "tampering"), ("wrongfix", "wrong_fix")]
)
async def test_a_scripted_failure_of_a_known_kind_gets_that_cause(alpha, tmp_path: Path, agent: str, cause: str) -> None:
    result = await run_suite(alpha, AgentSpec(agent), out_dir=tmp_path / "runs")

    (failed,) = result.tasks["alpha"].trials

    assert failed.passed is False and classify(failed).cause == cause
    assert failed.trace["model_calls"] >= 2  # the agent's own trace was found in its throwaway HOME


async def test_a_wrong_fix_shows_in_its_trace_that_it_ran_the_tests_and_still_failed(alpha, tmp_path: Path) -> None:
    result = await run_suite(alpha, AgentSpec("wrongfix"), out_dir=tmp_path / "runs")

    (failed,) = result.tasks["alpha"].trials
    facts = TraceFacts.from_dict(failed.trace)

    assert (facts.edits, facts.first_edit_turn, facts.tests_run) == (1, 1, 1)
    assert "ran the tests itself (1x)" in classify(failed).evidence


async def test_the_oracle_passes_and_its_trace_shows_the_fix_and_the_test_run(alpha, tmp_path: Path) -> None:
    result = await run_suite(alpha, AgentSpec("oracle"), out_dir=tmp_path / "runs")

    (solved,) = result.tasks["alpha"].trials
    facts = TraceFacts.from_dict(solved.trace)

    assert solved.passed and classify(solved) is None
    assert facts.edits >= 1 and facts.tests_run == 1 and facts.time_ms["total"] > 0


async def test_the_report_counts_the_causes(alpha, tmp_path: Path) -> None:
    result = await run_suite(alpha, AgentSpec("noop"), out_dir=tmp_path / "runs")

    assert "failure causes: no_attempt x1" in format_report(result)


async def test_a_baseline_keeps_the_counts_but_not_the_times(alpha, tmp_path: Path) -> None:
    result = await run_suite(alpha, AgentSpec("wrongfix"), out_dir=tmp_path / "runs")

    (kept,) = result.normalized().tasks["alpha"].trials
    (original,) = result.tasks["alpha"].trials

    assert "time_ms" in original.trace and "time_ms" not in kept.trace
    assert kept.trace["tests_run"] == 1 and kept.trace["edits"] == 1


async def test_explain_gives_the_cause_the_evidence_and_the_trace_tree(alpha, tmp_path: Path) -> None:
    result = await run_suite(alpha, AgentSpec("wrongfix"), out_dir=tmp_path / "runs")
    saved = tmp_path / "results.json"
    result.save(saved)

    text = runner.invoke(cli.app, ["eval", "explain", str(saved)]).output

    assert "alpha  trial 1  FAILED  cause: wrong_fix" in text
    assert "the agent changed files but the hidden tests still fail" in text
    assert "- ran the tests itself (1x)" in text
    assert "tool.call write_file" in text and "tool.call bash" in text and "where the time went" in text


def test_explain_says_so_when_nothing_failed_and_rejects_an_unknown_task(tmp_path: Path) -> None:
    saved = tmp_path / "results.json"
    suite({"a": [trial("a", 1, True)]}).save(saved)

    nothing = runner.invoke(cli.app, ["eval", "explain", str(saved)])
    passed = runner.invoke(cli.app, ["eval", "explain", str(saved), "--task", "a"])
    unknown = runner.invoke(cli.app, ["eval", "explain", str(saved), "--task", "nope"])

    assert nothing.exit_code == 0 and "No failed trials." in nothing.output
    assert passed.exit_code == 0 and "a  trial 1  passed" in passed.output
    assert unknown.exit_code == 2


def test_explain_still_works_when_the_trace_files_are_gone(tmp_path: Path) -> None:
    failed = trial("a", 1, False, files_changed=1, run_dir=str(tmp_path / "moved-away"))

    text = explain(failed)

    assert "cause: wrong_fix" in text and "tool.call" not in text
