"""Grading: run the hidden tests against whatever the agent left behind."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from seawall.evals.task import Task
from seawall.evals.workspace import overlay_grader


@dataclass(frozen=True)
class GradeResult:
    passed: bool
    returncode: int | None
    output_tail: str
    duration_seconds: float
    timed_out: bool = False


def clean_env(home: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for subprocesses: no inherited secrets, an isolated HOME."""
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TERM": "dumb",
    }
    env.update(extra or {})
    return env


async def grade(task: Task, workspace: Path, home: Path, *, tail_chars: int = 2000) -> GradeResult:
    """Copy the hidden tests into the workspace and run the task's grading command."""
    overlay_grader(task, workspace)
    command = [sys.executable if token == "{python}" else token for token in task.grading.command]
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=clean_env(home),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=task.grading.timeout_seconds)
    except asyncio.TimeoutError:
        _kill_group(process)
        await process.wait()
        return GradeResult(False, None, "grading timed out", time.monotonic() - started, timed_out=True)
    text = output.decode("utf-8", errors="replace")
    return GradeResult(
        passed=process.returncode == 0,
        returncode=process.returncode,
        output_tail=text[-tail_chars:],
        duration_seconds=time.monotonic() - started,
    )


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
