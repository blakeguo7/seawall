"""Shared test fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

from seawall.tasks.manager import shutdown_task_manager


_AMBIENT_PREFIXES = ("ANTHROPIC_", "OPENAI_", "DASHSCOPE_", "DEEPSEEK_", "SEAWALL_", "CLAUDE_CODE_")
_REAL_STATE_DIR = Path(os.environ.get("HOME", "~")).expanduser() / ".seawall"  # read before any test changes HOME
_PROJECT_STATE_DIR = Path(__file__).resolve().parents[1] / ".seawall"  # what a run inside the repo would create


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """Every file and directory under ``root`` (with a file's size and mtime); empty if it does not exist."""
    if not root.exists():
        return {}
    entries = {".": (0, 0)}
    for path in root.rglob("*"):
        stat = path.stat()
        entries[str(path.relative_to(root))] = (0, 0) if path.is_dir() else (stat.st_size, stat.st_mtime_ns)
    return entries


@pytest.fixture(scope="session", autouse=True)
def _state_directories_stay_untouched():
    """Fail the run if any test wrote into the developer's real ``~/.seawall`` or into the repo's own.

    The per-test fixture below points HOME at a temporary directory and starts each test in an empty
    one, but a test that undoes it (``monkeypatch.undo()`` undoes every patch of the test, the
    fixture's included) or spawns a process with the real HOME gets past it quietly. Looking up a
    project's plugins creates ``<cwd>/.seawall``, so a run from the repo root would leave one
    behind. This is the check that notices either.
    """
    watched = {"the real home's .seawall": _REAL_STATE_DIR, "the repo's .seawall": _PROJECT_STATE_DIR}
    before = {name: _fingerprint(path) for name, path in watched.items()}
    yield
    problems = []
    for name, path in watched.items():
        after = _fingerprint(path)
        changed = sorted(entry for entry in after.keys() | before[name].keys() if before[name].get(entry) != after.get(entry))
        if changed:
            problems.append(f"{name} ({path}) changed: {changed[:3]}")
    assert not problems, "a test is not isolated: " + "; ".join(problems)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the real home directory.

    Without this, tests that touch the default config/data locations write into
    ``~/.seawall`` and can pick up the developer's own skills and settings.
    Provider and model variables are dropped too: a developer's own
    ``ANTHROPIC_MODEL`` or ``OPENAI_API_KEY`` must not change what a test sees.
    Tests that need specific directories or variables still set them.
    """
    for name in list(os.environ):
        if name.startswith(_AMBIENT_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    # Tests only talk to servers on this machine. Without this, a system-wide proxy (macOS reads
    # one even when no variable is set) answers requests for 127.0.0.1 with a 502.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(home / ".seawall"))
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(home / ".seawall" / "data"))
    monkeypatch.setenv("SEAWALL_LOGS_DIR", str(home / ".seawall" / "logs"))
    # Start every test in an empty directory. Looking up a project's plugins or config creates
    # <cwd>/.seawall, and a test that forgot to pass a cwd would otherwise create it in the repo.
    monkeypatch.chdir(home)
    yield home


@pytest_asyncio.fixture(autouse=True)
async def _reset_background_task_manager():
    yield
    await shutdown_task_manager()
