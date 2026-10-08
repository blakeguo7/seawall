"""Fresh workspaces for trials, and knowing what the agent changed in them."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from seawall.evals.task import GRADER_DIR_NAME, Task

_IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git", ".mypy_cache", ".ruff_cache", ".venv", "node_modules"}


def hash_tree(root: Path) -> dict[str, str]:
    """Map every file under ``root`` (relative posix path) to its SHA-256, skipping caches."""
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_DIRS or part.endswith(".pyc") for part in relative.parts):
            continue
        hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def materialize(task: Task, workspace: Path) -> dict[str, str]:
    """Copy the task's starting repo into ``workspace`` and return its file hashes."""
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.repo_dir, workspace, dirs_exist_ok=True, symlinks=False)
    return hash_tree(workspace)


def overlay_solution(task: Task, workspace: Path) -> None:
    """Apply the reference solution (whole files) over the workspace."""
    shutil.copytree(task.solution_dir, workspace, dirs_exist_ok=True)


def overlay_grader(task: Task, workspace: Path) -> Path:
    """Put the hidden tests into the workspace, replacing anything the agent left there."""
    target = workspace / GRADER_DIR_NAME
    if target.exists() or target.is_symlink():
        shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
    shutil.copytree(task.grader_dir, target)
    return target


@dataclass(frozen=True)
class Changes:
    """What differs between two snapshots of a workspace."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


def diff_trees(before: dict[str, str], after: dict[str, str]) -> Changes:
    return Changes(
        added=tuple(sorted(set(after) - set(before))),
        modified=tuple(sorted(p for p in set(before) & set(after) if before[p] != after[p])),
        deleted=tuple(sorted(set(before) - set(after))),
    )


def protected_changes(task: Task, workspace: Path, before: dict[str, str]) -> list[str]:
    """Files matching the task's ``protected`` patterns that the agent changed or deleted."""
    touched: list[str] = []
    after = hash_tree(workspace)
    for pattern in task.protected:
        for original in sorted(Path(task.repo_dir).glob(pattern)):
            relative = original.relative_to(task.repo_dir).as_posix()
            if relative in before and after.get(relative) != before[relative]:
                touched.append(relative)
    return sorted(set(touched))
