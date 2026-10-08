"""Workspaces, change tracking, grading and task validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from seawall.evals.grader import clean_env, grade
from seawall.evals.runner import validate_task
from seawall.evals.task import GRADER_DIR_NAME, load_task
from seawall.evals.workspace import (
    diff_trees,
    hash_tree,
    materialize,
    overlay_grader,
    overlay_solution,
    protected_changes,
)
from tests.test_evals.conftest import make_task


@pytest.fixture
def task(tasks_root: Path):
    make_task(
        tasks_root,
        repo={"calc.py": "def add(a, b):\n    return a - b\n", "tests/test_calc.py": "from calc import add\n\n\ndef test():\n    assert add(1, 1) == 2\n"},
        protected=["tests/test_calc.py"],
    )
    return load_task(tasks_root / "demo")


def test_hash_tree_skips_caches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"x")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "v").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("b")

    assert sorted(hash_tree(tmp_path)) == ["a.py", "sub/b.py"]


def test_materialize_copies_the_repo_fresh_each_time(task, tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    before = materialize(task, first)
    (first / "calc.py").write_text("changed")
    materialize(task, second)

    assert sorted(before) == ["calc.py", "tests/test_calc.py"]
    assert (second / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
    assert not (second / GRADER_DIR_NAME).exists()  # hidden tests are not in the agent's workspace


def test_diff_trees() -> None:
    changes = diff_trees({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "d": "4"})
    assert (changes.added, changes.modified, changes.deleted) == (("d",), ("b",), ("c",))
    assert changes.total == 3


def test_protected_changes(task, tmp_path: Path) -> None:
    before = materialize(task, tmp_path)
    assert protected_changes(task, tmp_path, before) == []
    (tmp_path / "calc.py").write_text("fixed")  # not protected
    assert protected_changes(task, tmp_path, before) == []
    (tmp_path / "tests" / "test_calc.py").write_text("def test():\n    pass\n")
    assert protected_changes(task, tmp_path, before) == ["tests/test_calc.py"]
    (tmp_path / "tests" / "test_calc.py").unlink()  # deleting counts too
    assert protected_changes(task, tmp_path, before) == ["tests/test_calc.py"]


def test_the_grader_replaces_whatever_the_agent_left_in_its_slot(task, tmp_path: Path) -> None:
    materialize(task, tmp_path)
    planted = tmp_path / GRADER_DIR_NAME
    planted.mkdir()
    (planted / "test_hidden.py").write_text("def test_hidden():\n    assert True\n")  # an agent trying to pre-empt the tests
    overlay_grader(task, tmp_path)
    assert "add(2, 3) == 5" in (planted / "test_hidden.py").read_text()


async def test_grading_fails_then_passes_with_the_solution(task, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    materialize(task, workspace)

    broken = await grade(task, workspace, home)
    assert broken.passed is False and broken.returncode == 1
    assert "assert" in broken.output_tail

    overlay_solution(task, workspace)
    fixed = await grade(task, workspace, home)
    assert fixed.passed is True and fixed.returncode == 0


async def test_grading_times_out(tasks_root: Path, tmp_path: Path) -> None:
    make_task(
        tasks_root,
        "slow",
        grader={"test_hidden.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"},
        grading={"command": ["{python}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "_grader"], "timeout_seconds": 1},
    )
    task = load_task(tasks_root / "slow")
    workspace = tmp_path / "ws"
    materialize(task, workspace)
    result = await grade(task, workspace, tmp_path)
    assert result.timed_out is True and result.passed is False


async def test_a_sound_task_validates(task, tmp_path: Path) -> None:
    assert await validate_task(task, tmp_path) == []


async def test_a_task_that_already_passes_is_reported(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "trivial", repo={"calc.py": "def add(a, b):\n    return a + b\n"})
    problems = await validate_task(load_task(tasks_root / "trivial"), tmp_path)
    assert any("untouched repo" in p for p in problems)


async def test_a_task_its_own_solution_cannot_pass_is_reported(tasks_root: Path, tmp_path: Path) -> None:
    make_task(tasks_root, "impossible", solution={"calc.py": "def add(a, b):\n    return 0\n"})
    problems = await validate_task(load_task(tasks_root / "impossible"), tmp_path)
    assert any("reference solution" in p for p in problems)


def test_the_clean_environment_leaks_nothing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = clean_env(tmp_path)
    assert "sk-secret" not in " ".join(env.values())
    assert env["HOME"] == str(tmp_path)
    assert set(env) >= {"PATH", "HOME", "LANG"}
