"""Background cron scheduler daemon.

Runs as a standalone process (``seawall cron start``) or can be embedded via
:func:`run_scheduler_loop`.  Every tick it reads the cron registry, checks
which enabled jobs are due, executes them, and records results in a history
log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import shlex
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable

from seawall.config.paths import get_data_dir, get_logs_dir
from seawall.platforms import get_platform
from seawall.services.cron import (
    load_cron_jobs,
    mark_job_run,
    validate_cron_expression,
)
from seawall.sandbox import SandboxUnavailableError
from seawall.utils.shell import create_shell_subprocess

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
"""How often the scheduler checks for due jobs."""


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def get_history_path() -> Path:
    """Return the path to the cron execution history file."""
    return get_data_dir() / "cron_history.jsonl"


def append_history(entry: dict[str, Any]) -> None:
    """Append one execution record to the history log."""
    path = get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def load_history(*, limit: int = 50, job_name: str | None = None) -> list[dict[str, Any]]:
    """Load the most recent execution history entries."""
    path = get_history_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job_name and entry.get("name") != job_name:
            continue
        entries.append(entry)
    return entries[-limit:]


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def get_pid_path() -> Path:
    """Return the scheduler PID file path."""
    return get_data_dir() / "cron_scheduler.pid"


def read_pid() -> int | None:
    """Read the PID of a running scheduler, or None."""
    path = get_pid_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if not _pid_exists(pid):
        logger.debug("Removed stale scheduler PID file (pid=%d)", pid)
        path.unlink(missing_ok=True)
        return None
    return pid


def write_pid() -> None:
    """Write the current process PID."""
    path = get_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def remove_pid() -> None:
    """Remove the PID file."""
    get_pid_path().unlink(missing_ok=True)


def is_scheduler_running() -> bool:
    """Return True if a scheduler process is alive."""
    return read_pid() is not None


def stop_scheduler() -> bool:
    """Send SIGTERM to the running scheduler. Returns True if killed."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        _terminate_pid(pid)
    except OSError:
        remove_pid()
        return False
    # Wait briefly for process to exit
    for _ in range(10):
        if not _pid_exists(pid):
            remove_pid()
            return True
        time.sleep(0.2)
    # Force kill
    try:
        _kill_pid(pid)
    except OSError:
        pass
    remove_pid()
    return True


def _pid_exists(pid: int) -> bool:
    """Return True when *pid* currently refers to a live process."""
    if pid <= 0:
        return False
    if get_platform() == "windows":
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_exists(pid: int) -> bool:
    """Windows implementation of ``kill(pid, 0)`` without requiring psutil."""
    try:
        import ctypes
    except Exception:
        return _pid_exists_with_kill_zero(pid)

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except AttributeError:
        return _pid_exists_with_kill_zero(pid)

    synchronize = 0x00100000
    process_query_limited_information = 0x1000
    wait_timeout = 0x00000102

    handle = kernel32.OpenProcess(
        synchronize | process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: process exists
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _pid_exists_with_kill_zero(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _kill_pid(pid: int) -> None:
    if get_platform() == "windows":
        os.kill(pid, signal.SIGTERM)
        return
    os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


def _command_for_job(job: dict[str, Any]) -> str:
    """Return the shell command used to execute a job."""
    command = job.get("command")
    if command:
        return str(command)
    payload = job.get("payload")
    if not isinstance(payload, dict) or payload.get("kind", "agent_turn") != "agent_turn":
        raise ValueError("cron job has no command or agent_turn payload")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise ValueError("agent_turn cron job is missing payload.message")
    parts = [sys.executable, "-m", "seawall", "--print", message]
    return " ".join(shlex.quote(part) for part in parts)


def _env_for_job(job: dict[str, Any]) -> dict[str, str] | None:
    """Return the environment for a job, selecting its provider profile if it names one."""
    payload = job.get("payload")
    profile = (payload.get("profile") if isinstance(payload, dict) else None) or job.get("provider_profile")
    if not profile:
        return None
    return {**os.environ, "SEAWALL_PROFILE": str(profile)}


async def execute_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run a single cron job and return a history entry."""
    name = job["name"]
    cwd = Path(job.get("cwd") or ".").expanduser()
    started_at = datetime.now(timezone.utc)
    try:
        command = _command_for_job(job)
    except Exception as exc:
        entry = {
            "name": name,
            "command": "",
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry

    logger.info("Executing cron job %r: %s", name, command)
    try:
        process = await create_shell_subprocess(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env_for_job(job),
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=300,
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "timeout",
            "stdout": "",
            "stderr": "Job timed out after 300s",
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry
    except SandboxUnavailableError as exc:
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry
    except Exception as exc:
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry

    success = process.returncode == 0
    entry = {
        "name": name,
        "command": command,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": process.returncode,
        "status": "success" if success else "failed",
        "stdout": (stdout.decode("utf-8", errors="replace")[-2000:] if stdout else ""),
        "stderr": (stderr.decode("utf-8", errors="replace")[-2000:] if stderr else ""),
    }
    mark_job_run(name, success=success)
    append_history(entry)
    logger.info("Job %r finished: %s (rc=%s)", name, entry["status"], process.returncode)
    return entry


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _jobs_due(jobs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Return jobs whose next_run is at or before *now*."""
    due: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        schedule = job.get("schedule", "")
        if not validate_cron_expression(schedule):
            continue
        next_run_str = job.get("next_run")
        if not next_run_str:
            continue
        try:
            next_run = datetime.fromisoformat(next_run_str)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if next_run <= now:
            due.append(job)
    return due


async def run_scheduler_loop(*, once: bool = False) -> None:
    """Main scheduler loop.  Runs until SIGTERM or *once* is True (test mode)."""
    shutdown = asyncio.Event()

    loop = asyncio.get_running_loop()
    restore_signals = _install_shutdown_signal_handlers(loop, shutdown)

    write_pid()
    logger.info("Cron scheduler started (pid=%d, tick=%ds)", os.getpid(), TICK_INTERVAL_SECONDS)

    try:
        while not shutdown.is_set():
            now = datetime.now(timezone.utc)
            jobs = load_cron_jobs()
            due = _jobs_due(jobs, now)

            if due:
                logger.info("Tick: %d job(s) due", len(due))
                # Execute due jobs concurrently
                results = await asyncio.gather(
                    *(execute_job(job) for job in due), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        logger.error("Unexpected error executing cron job: %s", result)

            if once:
                break

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TICK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        restore_signals()
        remove_pid()
        logger.info("Cron scheduler stopped")


def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown: asyncio.Event,
) -> Callable[[], None]:
    """Install portable signal handlers and return a restore callback."""
    previous_handlers: list[tuple[signal.Signals, Any]] = []

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        del frame
        logger.info("Received shutdown signal (%s)", signum)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(shutdown.set)

    signals: list[signal.Signals] = [signal.SIGTERM, signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)

    for sig in signals:
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, _on_signal)
        except (OSError, RuntimeError, ValueError):
            continue
        previous_handlers.append((sig, previous))

    def _restore() -> None:
        for sig, previous in reversed(previous_handlers):
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                signal.signal(sig, previous)

    return _restore


# ---------------------------------------------------------------------------
# Daemon entry point (spawned by ``seawall cron start``)
# ---------------------------------------------------------------------------

def _run_daemon() -> None:
    """Entry point for the scheduler subprocess."""
    log_file = get_logs_dir() / "cron_scheduler.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run_scheduler_loop())


def start_daemon() -> int:
    """Start the scheduler daemon and return its PID."""
    existing = read_pid()
    if existing is not None:
        raise RuntimeError(f"Scheduler already running (pid={existing})")

    process = _spawn_scheduler_process()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pid = read_pid()
        if pid is not None:
            return pid
        if process.poll() is not None:
            break
        time.sleep(0.1)

    if process.poll() is not None:
        log_file = get_logs_dir() / "cron_scheduler.log"
        raise RuntimeError(f"Cron scheduler failed to start; see {log_file}")
    return process.pid


def _spawn_scheduler_process() -> subprocess.Popen[bytes]:
    """Spawn a detached scheduler subprocess on Unix and Windows."""
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if get_platform() == "windows":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
        if creationflags:
            popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-m", "seawall.services.cron_scheduler"],
        **popen_kwargs,
    )


def scheduler_status() -> dict[str, Any]:
    """Return a status dict about the scheduler."""
    pid = read_pid()
    log_path = get_logs_dir() / "cron_scheduler.log"
    jobs = load_cron_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    return {
        "running": pid is not None,
        "pid": pid,
        "total_jobs": len(jobs),
        "enabled_jobs": len(enabled),
        "log_file": str(log_path),
        "history_file": str(get_history_path()),
    }


if __name__ == "__main__":
    _run_daemon()
