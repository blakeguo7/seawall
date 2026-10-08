"""Helpers for building tiny eval tasks in temporary directories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

GRADE = ["{python}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "_grader"]


def make_task(
    root: Path,
    task_id: str = "demo",
    *,
    repo: dict[str, str] | None = None,
    grader: dict[str, str] | None = None,
    solution: dict[str, str] | None = None,
    **spec,
) -> Path:
    """Write a task directory: a one-function bug to fix, with hidden tests."""
    repo = repo if repo is not None else {"calc.py": "def add(a, b):\n    return a - b\n"}
    grader = grader if grader is not None else {
        "test_hidden.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    }
    solution = solution if solution is not None else {"calc.py": "def add(a, b):\n    return a + b\n"}
    directory = root / task_id
    for name, files in (("repo", repo), ("grader", grader), ("solution", solution)):
        for relative, text in files.items():
            path = directory / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    body = {
        "id": task_id,
        "title": "Fix add",
        "prompt": "add() is wrong. Fix it.",
        "grading": {"command": GRADE, "timeout_seconds": 60},
    }
    body.update(spec)
    (directory / "task.json").write_text(json.dumps(body))
    return directory


@pytest.fixture
def tasks_root(tmp_path: Path) -> Path:
    root = tmp_path / "tasks"
    root.mkdir()
    return root


REPO_TASKS = Path(__file__).resolve().parents[2] / "evals" / "tasks"
