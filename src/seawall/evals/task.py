"""Coding tasks: a workspace, a prompt, and tests the agent never sees.

A task is a directory::

    my-task/
      task.json     id, prompt, limits, how to grade
      repo/         the starting workspace, copied fresh for every trial
      grader/       hidden tests, copied in only after the agent has finished
      solution/     a reference solution, as whole files overlaid on repo/

``solution/`` is what makes a task trustworthy: ``validate_task`` checks that grading
fails on the untouched repo and passes once the solution is applied, so a task cannot be
trivially passing or impossible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_TOOLS = ("bash", "read_file", "write_file", "edit_file", "glob", "grep")
GRADER_DIR_NAME = "_grader"  # where the hidden tests land inside the workspace
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REQUIRED = ("id", "title", "prompt", "grading")


class TaskError(ValueError):
    """A task directory is malformed."""


@dataclass(frozen=True)
class GradingSpec:
    """The command whose exit status decides pass or fail, run inside the workspace.

    ``{python}`` stands for the interpreter running the harness.
    """

    command: tuple[str, ...]
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    path: Path
    grading: GradingSpec
    tags: tuple[str, ...] = ()
    difficulty: str = "medium"
    max_turns: int = 30
    timeout_seconds: float = 300.0
    protected: tuple[str, ...] = ()  # workspace-relative globs the agent must not modify
    allowed_tools: tuple[str, ...] = DEFAULT_TOOLS

    @property
    def repo_dir(self) -> Path:
        return self.path / "repo"

    @property
    def grader_dir(self) -> Path:
        return self.path / "grader"

    @property
    def solution_dir(self) -> Path:
        return self.path / "solution"


def load_task(path: Path) -> Task:
    """Read and validate one task directory."""
    path = Path(path)
    spec_path = path / "task.json"
    if not spec_path.is_file():
        raise TaskError(f"{path}: no task.json")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TaskError(f"{spec_path}: not valid JSON ({exc})") from exc
    if not isinstance(spec, dict):
        raise TaskError(f"{spec_path}: expected a JSON object")
    missing = [key for key in _REQUIRED if key not in spec]
    if missing:
        raise TaskError(f"{spec_path}: missing {', '.join(missing)}")

    task_id = str(spec["id"])
    if not _ID.match(task_id):
        raise TaskError(f"{spec_path}: id {task_id!r} must be lowercase letters, digits and hyphens")
    if task_id != path.name:
        raise TaskError(f"{spec_path}: id {task_id!r} does not match the directory name {path.name!r}")
    for key in ("title", "prompt"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise TaskError(f"{spec_path}: {key} must be a non-empty string")

    grading = spec["grading"]
    command = grading.get("command") if isinstance(grading, dict) else None
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        raise TaskError(f"{spec_path}: grading.command must be a non-empty list of strings")

    task = Task(
        id=task_id,
        title=spec["title"].strip(),
        prompt=spec["prompt"].strip(),
        path=path,
        grading=GradingSpec(
            command=tuple(command), timeout_seconds=float(grading.get("timeout_seconds", 120))
        ),
        tags=tuple(str(t) for t in spec.get("tags", ())),
        difficulty=str(spec.get("difficulty", "medium")),
        max_turns=int(spec.get("max_turns", 30)),
        timeout_seconds=float(spec.get("timeout_seconds", 300)),
        protected=tuple(str(p) for p in spec.get("protected", ())),
        allowed_tools=tuple(str(t) for t in spec.get("allowed_tools", DEFAULT_TOOLS)),
    )
    _check_layout(task)
    return task


def load_tasks(root: Path, ids: list[str] | None = None) -> list[Task]:
    """Load every task under ``root`` (or only ``ids``), sorted by id."""
    root = Path(root)
    if not root.is_dir():
        raise TaskError(f"{root}: not a directory")
    tasks = [load_task(child) for child in sorted(root.iterdir()) if (child / "task.json").is_file()]
    if ids:
        known = {task.id for task in tasks}
        unknown = [task_id for task_id in ids if task_id not in known]
        if unknown:
            raise TaskError(f"unknown task(s): {', '.join(unknown)}")
        tasks = [task for task in tasks if task.id in set(ids)]
    return tasks


def _check_layout(task: Task) -> None:
    for name, directory in (("repo", task.repo_dir), ("grader", task.grader_dir), ("solution", task.solution_dir)):
        if not directory.is_dir():
            raise TaskError(f"{task.id}: missing {name}/ directory")
        if not any(p.is_file() for p in directory.rglob("*")):
            raise TaskError(f"{task.id}: {name}/ has no files")
        for entry in directory.rglob("*"):
            if entry.is_symlink():
                raise TaskError(f"{task.id}: {entry.relative_to(task.path)} is a symlink")
    for entry in task.solution_dir.rglob("*"):
        if entry.is_file():
            relative = PurePosixPath(entry.relative_to(task.solution_dir).as_posix())
            if relative.is_absolute() or ".." in relative.parts:
                raise TaskError(f"{task.id}: solution path {relative} escapes the workspace")
    if (task.repo_dir / GRADER_DIR_NAME).exists():
        raise TaskError(f"{task.id}: repo/ must not contain {GRADER_DIR_NAME}/, it is reserved for the grader")
    for pattern in task.protected:
        if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
            raise TaskError(f"{task.id}: protected pattern {pattern!r} must be relative to the workspace")
        if not any(task.repo_dir.glob(pattern)):
            raise TaskError(f"{task.id}: protected pattern {pattern!r} matches nothing in repo/")
