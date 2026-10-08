"""Loading and validating task directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from seawall.evals.task import DEFAULT_TOOLS, TaskError, load_task, load_tasks
from tests.test_evals.conftest import make_task


def test_a_well_formed_task_loads(tasks_root: Path) -> None:
    make_task(tasks_root, tags=["bugfix"], difficulty="easy", max_turns=9, timeout_seconds=42)
    task = load_task(tasks_root / "demo")

    assert (task.id, task.title, task.tags, task.difficulty) == ("demo", "Fix add", ("bugfix",), "easy")
    assert (task.max_turns, task.timeout_seconds) == (9, 42.0)
    assert task.allowed_tools == DEFAULT_TOOLS
    assert task.grading.timeout_seconds == 60
    assert task.repo_dir.name == "repo" and task.grader_dir.name == "grader" and task.solution_dir.name == "solution"


def test_defaults(tasks_root: Path) -> None:
    make_task(tasks_root)
    task = load_task(tasks_root / "demo")
    assert (task.max_turns, task.timeout_seconds, task.difficulty, task.tags) == (30, 300.0, "medium", ())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda spec: spec.pop("prompt"), "missing prompt"),
        (lambda spec: spec.pop("grading"), "missing grading"),
        (lambda spec: spec.update(id="Bad_Id"), "lowercase letters"),
        (lambda spec: spec.update(id="other"), "does not match the directory name"),
        (lambda spec: spec.update(prompt="  "), "prompt must be a non-empty string"),
        (lambda spec: spec.update(grading={"command": []}), "grading.command"),
        (lambda spec: spec.update(grading={"command": [1, 2]}), "grading.command"),
        (lambda spec: spec.update(protected=["nothing/here.py"]), "matches nothing"),
        (lambda spec: spec.update(protected=["../escape"]), "relative to the workspace"),
    ],
)
def test_malformed_specs(tasks_root: Path, mutate, message: str) -> None:
    directory = make_task(tasks_root)
    spec = json.loads((directory / "task.json").read_text())
    mutate(spec)
    (directory / "task.json").write_text(json.dumps(spec))
    with pytest.raises(TaskError, match=message):
        load_task(directory)


def test_missing_and_broken_task_json(tasks_root: Path) -> None:
    with pytest.raises(TaskError, match="no task.json"):
        load_task(tasks_root / "nothing")
    directory = make_task(tasks_root)
    (directory / "task.json").write_text("{nope")
    with pytest.raises(TaskError, match="not valid JSON"):
        load_task(directory)
    (directory / "task.json").write_text("[1]")
    with pytest.raises(TaskError, match="JSON object"):
        load_task(directory)


@pytest.mark.parametrize("name", ["repo", "grader", "solution"])
def test_each_directory_is_required_and_needs_files(tasks_root: Path, name: str) -> None:
    import shutil

    directory = make_task(tasks_root)
    shutil.rmtree(directory / name)
    with pytest.raises(TaskError, match=f"missing {name}/"):
        load_task(directory)
    (directory / name).mkdir()
    with pytest.raises(TaskError, match=f"{name}/ has no files"):
        load_task(directory)


def test_symlinks_are_refused(tasks_root: Path) -> None:
    directory = make_task(tasks_root)
    os.symlink("/etc/hosts", directory / "repo" / "link")
    with pytest.raises(TaskError, match="symlink"):
        load_task(directory)


def test_the_grader_directory_name_is_reserved(tasks_root: Path) -> None:
    directory = make_task(tasks_root, repo={"calc.py": "x = 1\n", "_grader/test_x.py": "pass\n"})
    with pytest.raises(TaskError, match="reserved"):
        load_task(directory)


def test_load_tasks_sorts_and_filters(tasks_root: Path) -> None:
    for name in ("zeta", "alpha", "mid"):
        make_task(tasks_root, name)
    (tasks_root / "not-a-task").mkdir()  # directories without task.json are ignored

    assert [t.id for t in load_tasks(tasks_root)] == ["alpha", "mid", "zeta"]
    assert [t.id for t in load_tasks(tasks_root, ["zeta", "alpha"])] == ["alpha", "zeta"]
    with pytest.raises(TaskError, match="unknown task"):
        load_tasks(tasks_root, ["alpha", "nope"])
    with pytest.raises(TaskError, match="not a directory"):
        load_tasks(tasks_root / "missing")
