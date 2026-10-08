"""grep and glob must not walk through credential files when pointed at a parent directory."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from seawall.permissions.sensitive import drop_sensitive, is_sensitive_path
from seawall.tools.base import ToolExecutionContext
from seawall.tools.glob_tool import GlobTool, GlobToolInput
from seawall.tools.grep_tool import GrepTool, GrepToolInput


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE-KEY-CANARY\n")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("SECRET-CANARY\n")
    (tmp_path / ".aws" / "notes.txt").write_text("harmless CANARY\n")  # same directory, not a credential
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "main.py").write_text("print('CANARY visible')\n")
    (tmp_path / ".gitignore").write_text("")  # makes ripgrep search hidden directories too
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def grep(home: Path, **kwargs) -> str:
    context = ToolExecutionContext(cwd=home)
    return run(GrepTool().execute(GrepToolInput(pattern="CANARY", root=str(home), **kwargs), context)).output


def glob(home: Path, pattern: str = "**/*") -> str:
    context = ToolExecutionContext(cwd=home)
    return run(GlobTool().execute(GlobToolInput(pattern=pattern, root=str(home)), context)).output


def test_grep_does_not_return_credential_files(home: Path) -> None:
    output = grep(home)
    assert "PRIVATE-KEY-CANARY" not in output
    assert "SECRET-CANARY" not in output
    assert "main.py" in output  # ordinary files are still searched


def test_grep_still_searches_other_hidden_files(home: Path) -> None:
    assert "notes.txt" in grep(home)


def test_glob_does_not_list_credential_files(home: Path) -> None:
    output = glob(home)
    assert "id_rsa" not in output
    assert ".aws/credentials" not in output
    assert "main.py" in output


def test_glob_with_the_python_fallback(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no ripgrep
    output = glob(home, "*")
    assert "id_rsa" not in output
    output = glob(home, ".ssh/*")
    assert "id_rsa" not in output


def test_grep_with_the_python_fallback(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    output = grep(home)
    assert "PRIVATE-KEY-CANARY" not in output and "SECRET-CANARY" not in output
    assert "main.py" in output


def test_helpers() -> None:
    assert is_sensitive_path("/home/u/.ssh/id_rsa")
    assert not is_sensitive_path("/home/u/project/main.py")
    root = Path("/home/u")
    assert drop_sensitive(root, [".ssh/id_rsa", "project/main.py", ".aws/credentials", "./.kube/config"]) == [
        "project/main.py"
    ]
