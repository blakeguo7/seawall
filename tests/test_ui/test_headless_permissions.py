"""Headless runs (``seawall -p`` and sub-agent workers) must not approve their own requests."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from seawall.audit import list_logs, read_records, verify_log
from seawall.audit.store import audit_dir
from seawall.config.settings import AuditSettings
from seawall.ui.app import run_print_mode, run_task_worker
from seawall.ui.overrides import SessionOverrides
from tests.fakes import ScriptedClient, text_message, tool_message


def script(command: str = "mkdir made") -> ScriptedClient:
    return ScriptedClient(tool_message(("bash", {"command": command})), text_message("finished"))


async def run_json(tmp_path: Path, capsys, client: ScriptedClient, **kwargs) -> dict:
    await run_print_mode(
        prompt="make a directory",
        output_format="json",
        cwd=str(tmp_path),
        api_client=client,
        **kwargs,
    )
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


async def test_default_mode_refuses_what_needs_approval(tmp_path: Path, capsys) -> None:
    result = await run_json(tmp_path, capsys, script())

    assert not (tmp_path / "made").exists()
    (denial,) = result["permission_denials"]
    assert denial["tool_name"] == "bash"
    assert denial["tool_input"] == {"command": "mkdir made"}
    assert denial["denied_by"] == "approval"
    assert "--permission-mode full_auto" in denial["message"]
    assert result["text"] == "finished"


async def test_the_permission_mode_flag_is_honoured(tmp_path: Path, capsys) -> None:
    """``seawall -p --permission-mode full_auto`` used to be ignored in print mode."""
    result = await run_json(tmp_path, capsys, script(), permission_mode="full_auto")
    assert (tmp_path / "made").is_dir()
    assert result["permission_denials"] == []


async def test_plan_mode_blocks_writes_in_print_mode(tmp_path: Path, capsys) -> None:
    result = await run_json(tmp_path, capsys, script(), permission_mode="plan")
    assert not (tmp_path / "made").exists()
    assert result["permission_denials"][0]["denied_by"] == "policy"


async def test_allowed_tools_lets_a_headless_run_act(tmp_path: Path, capsys) -> None:
    result = await run_json(tmp_path, capsys, script(), overrides=SessionOverrides.from_cli(allowed_tools=["bash"]))
    assert (tmp_path / "made").is_dir()
    assert result["permission_denials"] == []


async def test_disallowed_tools_beats_full_auto(tmp_path: Path, capsys) -> None:
    result = await run_json(
        tmp_path,
        capsys,
        script(),
        permission_mode="full_auto",
        overrides=SessionOverrides.from_cli(disallowed_tools=["bash"]),
    )
    assert not (tmp_path / "made").exists()
    assert result["permission_denials"][0]["denied_by"] == "policy"


async def test_critical_commands_stay_refused_in_full_auto(tmp_path: Path, capsys) -> None:
    result = await run_json(
        tmp_path, capsys, script("cat ~/.aws/credentials"), permission_mode="full_auto"
    )
    assert "critical risk" in result["permission_denials"][0]["message"]


async def test_text_mode_says_on_stderr_that_a_call_was_not_run(tmp_path: Path, capsys) -> None:
    await run_print_mode(
        prompt="make a directory", cwd=str(tmp_path), api_client=script(), output_format="text"
    )
    captured = capsys.readouterr()
    assert "[not run] bash" in captured.err
    assert captured.out.strip() == "finished"


async def test_stream_json_marks_denied_calls(tmp_path: Path, capsys) -> None:
    await run_print_mode(
        prompt="make a directory", cwd=str(tmp_path), api_client=script(), output_format="stream-json"
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    (completed,) = [e for e in events if e["type"] == "tool_completed"]
    assert completed["denied"] is True


async def test_append_system_prompt_is_added_to_the_prompt(tmp_path: Path, capsys) -> None:
    client = ScriptedClient(text_message("ok"))
    await run_json(
        tmp_path, capsys, client, overrides=SessionOverrides.from_cli(append_system_prompt="Always answer in Latin.")
    )
    system_prompt = client.requests[0].system_prompt
    assert system_prompt.rstrip().endswith("Always answer in Latin.")
    assert len(system_prompt) > len("Always answer in Latin.") + 200  # the default prompt is still there


async def test_unknown_tool_names_are_called_out(tmp_path: Path, capsys) -> None:
    await run_print_mode(
        prompt="hi",
        cwd=str(tmp_path),
        api_client=ScriptedClient(text_message("ok")),
        overrides=SessionOverrides.from_cli(allowed_tools=["Bsh"]),
    )
    assert "--allowed-tools lists tools that do not exist: Bsh" in capsys.readouterr().err


async def test_a_session_leaves_a_verifiable_audit_trail(tmp_path: Path, capsys) -> None:
    await run_json(tmp_path, capsys, script())

    (path,) = list_logs(audit_dir(AuditSettings()))
    result = verify_log(path)
    assert result.ok is True and result.closed is True
    records = list(read_records(path))
    assert [r["type"] for r in records] == [
        "session.start",
        "approval.requested",
        "approval.resolved",
        "tool.decision",
        "run.finished",
        "session.end",
    ]
    start = records[0]["data"]
    assert start["permission_mode"] == "default"
    assert start["cwd"] == str(tmp_path.resolve())


# --- sub-agent workers ---------------------------------------------------------------------------


@pytest.fixture
def worker_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("do the work\n"))


async def test_a_worker_cannot_approve_its_own_requests(tmp_path: Path, worker_stdin, capsys) -> None:
    """Workers used to auto-approve every confirmation, whatever mode the parent was in."""
    await run_task_worker(cwd=str(tmp_path), api_client=script(), max_turns=3)

    assert not (tmp_path / "made").exists()
    assert "finished" in capsys.readouterr().out


async def test_a_worker_acts_under_the_mode_it_was_given(tmp_path: Path, worker_stdin) -> None:
    await run_task_worker(
        cwd=str(tmp_path), api_client=script(), max_turns=3, permission_mode="full_auto"
    )
    assert (tmp_path / "made").is_dir()


async def test_a_worker_honours_inherited_tool_lists(tmp_path: Path, worker_stdin) -> None:
    await run_task_worker(
        cwd=str(tmp_path),
        api_client=script(),
        max_turns=3,
        overrides=SessionOverrides.from_cli(allowed_tools=["bash"]),
    )
    assert (tmp_path / "made").is_dir()
