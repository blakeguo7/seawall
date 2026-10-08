"""Tests for task and team tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from seawall.coordinator.coordinator_mode import get_team_registry
from seawall.tasks import get_task_manager
from seawall.tools.agent_tool import AgentTool, AgentToolInput
from seawall.tools.base import ToolExecutionContext
from seawall.tools.task_create_tool import TaskCreateTool, TaskCreateToolInput
from seawall.tools.task_output_tool import TaskOutputTool, TaskOutputToolInput
from seawall.tools.task_update_tool import TaskUpdateTool, TaskUpdateToolInput
from seawall.tools.team_create_tool import TeamCreateTool, TeamCreateToolInput


async def _wait_for_terminal_task(task_id: str, *, timeout_seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    manager = get_task_manager()
    while asyncio.get_running_loop().time() < deadline:
        task = manager.get_task(task_id)
        if task is not None and task.status in {"completed", "failed", "killed"}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Task {task_id} did not reach a terminal status in time")


@pytest.mark.asyncio
async def test_task_create_and_output_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="echo",
            command="printf 'tool task'",
        ),
        context,
    )
    assert create_result.is_error is False
    task_id = create_result.output.split()[2]

    manager = get_task_manager()
    for _ in range(20):
        if "tool task" in manager.read_task_output(task_id):
            break
        await asyncio.sleep(0.1)
    output_result = await TaskOutputTool().execute(
        TaskOutputToolInput(task_id=task_id),
        context,
    )
    assert "tool task" in output_result.output


@pytest.mark.asyncio
async def test_team_create_tool(tmp_path: Path):
    result = await TeamCreateTool().execute(
        TeamCreateToolInput(name="demo", description="test"),
        ToolExecutionContext(cwd=tmp_path),
    )
    assert result.is_error is False
    assert "Created team demo" == result.output


@pytest.mark.asyncio
async def test_task_update_tool_updates_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    create_result = await TaskCreateTool().execute(
        TaskCreateToolInput(
            type="local_bash",
            description="updatable",
            command="printf 'tool task'",
        ),
        context,
    )
    task_id = create_result.output.split()[2]

    update_result = await TaskUpdateTool().execute(
        TaskUpdateToolInput(
            task_id=task_id,
            progress=60,
            status_note="waiting on verification",
            description="renamed task",
        ),
        context,
    )
    assert update_result.is_error is False

    task = get_task_manager().get_task(task_id)
    assert task is not None
    assert task.description == "renamed task"
    assert task.metadata["progress"] == "60"
    assert task.metadata["status_note"] == "waiting on verification"


@pytest.mark.asyncio
async def test_agent_tool_uses_subprocess_backend_and_task_is_pollable(
    tmp_path: Path, monkeypatch
):
    """Regression test for #59 / PR #60.

    AgentTool must use the subprocess backend so the returned task_id is
    registered in BackgroundTaskManager and is queryable by the task tools.

    The returned task_id must be one BackgroundTaskManager knows about, so the
    task tools (TaskGet, TaskOutput, ...) can poll it without raising ValueError.
    """
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="backend regression check",
            prompt="hello",
            subagent_type="test-worker",
            # command echoes one line and exits — minimal subprocess
            command='python -u -c "import sys; print(sys.stdin.readline().strip())"',
        ),
        context,
    )

    assert not result.is_error, f"AgentTool failed: {result.output}"

    # 1. The backend reported in the output must be subprocess.
    assert "backend=subprocess" in result.output, (
        f"Expected backend=subprocess in output, got: {result.output}"
    )

    # 2. The task_id must be registered in BackgroundTaskManager so task tools
    #    can query it without raising ValueError.
    #    Parse task_id from "Spawned agent X (task_id=Y, backend=Z)"
    import re
    m = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert m, f"Could not parse task_id from output: {result.output}"
    task_id = m.group(1)

    manager = get_task_manager()
    record = manager.get_task(task_id)
    assert record is not None, (
        f"task_id {task_id!r} not found in BackgroundTaskManager — "
        "task tools (TaskGet, TaskOutput, etc.) would have failed"
    )
    assert record.command == 'python -u -c "import sys; print(sys.stdin.readline().strip())"'
    assert record.type == "local_agent"
    await _wait_for_terminal_task(task_id)


@pytest.mark.asyncio
async def test_send_message_agent_path_uses_subprocess_backend(
    tmp_path: Path, monkeypatch
):
    """SendMessageTool._send_agent_message must route via SubprocessBackend."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    from seawall.tools.send_message_tool import SendMessageTool

    with patch(
        "seawall.swarm.subprocess_backend.SubprocessBackend.send_message",
        new_callable=AsyncMock,
    ) as mock_send:
        await SendMessageTool().execute(
            __import__(
                "seawall.tools.send_message_tool",
                fromlist=["SendMessageToolInput"],
            ).SendMessageToolInput(
                task_id="worker@default",
                message="ping",
            ),
            context,
        )

    # send_message may raise ValueError because no agent was spawned yet
    # (no _agent_tasks entry), but the key assertion is that SubprocessBackend
    # was called.
    mock_send.assert_called_once()
    agent_id_arg = mock_send.call_args[0][0]
    assert agent_id_arg == "worker@default"


@pytest.mark.asyncio
async def test_agent_tool_creates_missing_team_when_team_argument_is_provided(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    get_team_registry()._teams.clear()
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="team auto-create regression",
            prompt="ready",
            subagent_type="test-worker-team",
            team="design-qa-loop",
            command="python -u -c \"import sys; print(sys.stdin.readline().strip())\"",
        ),
        context,
    )

    assert result.is_error is False
    teams = {team.name: team for team in get_team_registry().list_teams()}
    assert "design-qa-loop" in teams
    assert len(teams["design-qa-loop"].agents) == 1


@pytest.mark.asyncio
async def test_agent_tool_supports_remote_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(
            description="remote_agent smoke",
            prompt="ready",
            mode="remote_agent",
            subagent_type="test-worker-0",
            command="python -u -c \"import sys; print(sys.stdin.readline().strip())\"",
        ),
        context,
    )
    assert result.is_error is False
    import re

    match = re.search(r"task_id=(\S+?)[,)]", result.output)
    assert match, result.output
    task_id = match.group(1)
    record = get_task_manager().get_task(task_id)
    assert record is not None
    assert record.type == "remote_agent"
    await _wait_for_terminal_task(task_id)


@pytest.mark.asyncio
async def test_agent_tool_rejects_unknown_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SEAWALL_DATA_DIR", str(tmp_path / "data"))
    context = ToolExecutionContext(cwd=tmp_path)

    result = await AgentTool().execute(
        AgentToolInput(description="bad mode", prompt="ready", mode="in_process_teammate"),
        context,
    )
    assert result.is_error is True
    assert "Invalid mode" in result.output


@pytest.mark.asyncio
async def test_agent_tool_hands_the_sessions_permissions_to_the_worker(tmp_path: Path, monkeypatch):
    """A worker runs under the parent's mode and tool lists, never under broader ones."""
    from seawall.swarm.types import SpawnResult

    captured = {}

    class _Executor:
        async def spawn(self, config):
            captured["config"] = config
            return SpawnResult(task_id="task_x", agent_id="agent@default", backend_type="subprocess")

    class _Registry:
        def get_executor(self, name):
            return _Executor()

    monkeypatch.setattr("seawall.tools.agent_tool.get_backend_registry", lambda: _Registry())
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "permission_mode": "plan",
            "permission_allowed_tools": ["glob"],
            "permission_denied_tools": ["bash"],
        },
    )

    result = await AgentTool().execute(AgentToolInput(description="d", prompt="p"), context)

    assert result.is_error is False
    config = captured["config"]
    assert config.permission_mode == "plan"
    assert config.allowed_tools == ["glob"]
    assert config.disallowed_tools == ["bash"]
