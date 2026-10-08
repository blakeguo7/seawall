"""Whole trials: a real `seawall` subprocess against the scripted model, then grading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from seawall.evals.runner import AgentSpec, validate_task
from seawall.evals.suite import run_suite
from seawall.evals.task import load_task, load_tasks
from tests.test_evals.conftest import REPO_TASKS, make_task


@pytest.fixture
def two_tasks(tasks_root: Path):
    make_task(tasks_root, "alpha", protected=["tests/test_calc.py"], repo={"calc.py": "def add(a, b):\n    return a - b\n", "tests/test_calc.py": "from calc import add\n\n\ndef test():\n    assert add(1, 1) == 2\n"}, solution={"calc.py": "def add(a, b):\n    return a + b\n"})
    make_task(tasks_root, "beta")
    return load_tasks(tasks_root)


async def test_the_oracle_solves_tasks_end_to_end(two_tasks, tmp_path: Path) -> None:
    result = await run_suite(two_tasks, AgentSpec("oracle"), out_dir=tmp_path / "runs", jobs=2)

    assert set(result.tasks) == {"alpha", "beta"}
    for outcome in result.tasks.values():
        (trial,) = outcome.trials
        assert trial.passed and trial.grader_passed and trial.tampered_files == []
        assert trial.stop_reason == "completed" and trial.turns == 3
        assert trial.files_changed == 1
        assert trial.audit_verified is True
        assert trial.tool_calls_allowed == 2 and trial.tool_calls_denied == 0  # write_file, then pytest
        assert trial.cost_usd and trial.cost_usd > 0  # the scripted model has a price
        assert trial.usage["input_tokens"] > 0
        assert trial.exit_code == 0 and trial.error is None
    assert result.agent["kind"] == "oracle" and result.harness_version


async def test_the_noop_agent_fails_every_task(two_tasks, tmp_path: Path) -> None:
    result = await run_suite(two_tasks, AgentSpec("noop"), out_dir=tmp_path / "runs")
    for outcome in result.tasks.values():
        (trial,) = outcome.trials
        assert trial.passed is False and trial.grader_passed is False
        assert trial.files_changed == 0
        assert "assert" in trial.grader_output_tail


async def test_rewriting_the_visible_tests_is_caught(two_tasks, tmp_path: Path) -> None:
    result = await run_suite([t for t in two_tasks if t.id == "alpha"], AgentSpec("cheater"), out_dir=tmp_path / "runs")
    (trial,) = result.tasks["alpha"].trials
    assert trial.passed is False
    assert trial.tampered_files == ["tests/test_calc.py"]


async def test_trials_are_independent_and_numbered(two_tasks, tmp_path: Path) -> None:
    result = await run_suite(two_tasks[:1], AgentSpec("oracle"), trials=3, jobs=3, out_dir=tmp_path / "runs")
    trials = result.tasks["alpha"].trials
    assert [t.trial for t in trials] == [1, 2, 3]
    assert len({t.run_dir for t in trials}) == 3
    assert all(t.passed for t in trials)


async def test_a_relative_work_directory_works(two_tasks, tmp_path: Path, monkeypatch) -> None:
    """The agent runs in another directory, so relative paths in its environment must not leak through."""
    monkeypatch.chdir(tmp_path)
    result = await run_suite(two_tasks[:1], AgentSpec("oracle"), out_dir=Path("relative-runs"))
    (trial,) = result.tasks["alpha"].trials
    assert trial.passed and trial.audit_verified and os.path.isabs(trial.run_dir)


async def test_a_trial_that_cannot_run_is_reported_not_raised(two_tasks, tmp_path: Path, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("seawall.evals.suite.run_trial", boom)
    result = await run_suite(two_tasks, AgentSpec("oracle"), out_dir=tmp_path / "runs")
    for outcome in result.tasks.values():
        (trial,) = outcome.trials
        assert (trial.passed, trial.stop_reason) == (False, "harness_error")
        assert "disk on fire" in trial.error


async def test_an_agent_that_hangs_is_killed_at_the_task_timeout(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "hangs", timeout_seconds=1)
    task = load_task(tasks_root / "hangs")
    # Point the agent at a port nothing listens on, so its first model call never returns a reply
    # quickly; the harness must stop it, grade what is there, and say what happened.
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(0)  # accepts connections into the backlog but never answers
    try:
        from seawall.evals.runner import run_trial

        class Hang:
            def register(self, run_id, script):
                return f"http://127.0.0.1:{sock.getsockname()[1]}/{run_id}"

        trial = await run_trial(task, 1, AgentSpec("oracle"), out_dir=tmp_path / "runs", server=Hang())
    finally:
        sock.close()
    assert trial.stop_reason == "timeout" and trial.passed is False
    assert "did not finish within 1s" in trial.error


def test_unknown_agents_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        AgentSpec("wizard").validate()
    AgentSpec("live").validate()
    assert AgentSpec("live").is_live and not AgentSpec("oracle").is_live


# --- the shipped tasks ------------------------------------------------------------------------


def test_the_shipped_tasks_load() -> None:
    tasks = load_tasks(REPO_TASKS)
    assert len(tasks) >= 20
    assert len({t.id for t in tasks}) == len(tasks)
    for task in tasks:
        assert task.prompt and task.title and task.tags


async def test_every_shipped_task_is_sound(tmp_path: Path) -> None:
    """Each task fails as shipped and passes with its solution, so none is trivial or impossible."""
    import asyncio

    tasks = load_tasks(REPO_TASKS)
    results = await asyncio.gather(*(validate_task(t, tmp_path / t.id) for t in tasks))
    broken = {t.id: problems for t, problems in zip(tasks, results) if problems}
    assert broken == {}


# --- how a live agent is started ----------------------------------------------------------------


def test_a_live_agent_gets_its_key_from_the_environment_and_nothing_else(tasks_root: Path, monkeypatch) -> None:
    from seawall.evals.runner import agent_flags

    make_task(tasks_root)
    task = load_task(tasks_root / "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)  # this machine's own proxy settings are not part of the test

    spec = AgentSpec("live", model="my-model", base_url="https://api.example.com", api_format="openai",
                     max_budget_usd=1.5, max_total_tokens=50_000)
    flags, env = agent_flags(task, spec, base_url=None)

    assert env == {"ANTHROPIC_API_KEY": "sk-live", "OPENAI_API_KEY": "sk-openai"}  # provider keys only
    assert "--api-key" not in flags and "sk-live" not in " ".join(flags)  # never on the command line
    assert flags[flags.index("--model") + 1] == "my-model"
    assert flags[flags.index("--base-url") + 1] == "https://api.example.com"
    assert flags[flags.index("--api-format") + 1] == "openai"
    assert flags[flags.index("--max-budget-usd") + 1] == "1.5"
    assert flags[flags.index("--max-total-tokens") + 1] == "50000"
    assert flags[flags.index("--allowed-tools") + 1] == ",".join(task.allowed_tools)


def test_a_live_agent_keeps_the_proxy_settings_it_needs_to_reach_its_provider(tasks_root: Path, monkeypatch) -> None:
    from seawall.evals.runner import agent_flags

    make_task(tasks_root)
    task = load_task(tasks_root / "demo")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example")
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    _, env = agent_flags(task, AgentSpec("live", model="m"), base_url=None)
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128" and env["NO_PROXY"] == "internal.example"
    assert "SOMETHING_ELSE" not in env


def test_a_scripted_agent_gets_the_fake_endpoint_and_is_kept_off_proxies(tasks_root: Path) -> None:
    from seawall.evals.runner import LOOPBACK_DIRECT, agent_flags

    make_task(tasks_root)
    task = load_task(tasks_root / "demo")
    flags, env = agent_flags(task, AgentSpec("oracle", permission_mode="full_auto"), base_url="http://127.0.0.1:9/run")
    assert env == LOOPBACK_DIRECT  # only that: no keys, nothing inherited
    assert "127.0.0.1" in env["NO_PROXY"] and "localhost" in env["no_proxy"]
    assert flags[flags.index("--base-url") + 1] == "http://127.0.0.1:9/run"
    assert flags[flags.index("--permission-mode") + 1] == "full_auto" and "--allowed-tools" not in flags
