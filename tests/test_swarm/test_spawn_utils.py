"""Tests for teammate spawn helper behavior."""

from __future__ import annotations

import sys

import pytest

from seawall.swarm.spawn_utils import (
    TEAMMATE_COMMAND_ENV_VAR,
    build_inherited_cli_flags,
    build_inherited_env_vars,
    get_teammate_command,
)


def test_get_teammate_command_prefers_current_interpreter(monkeypatch):
    monkeypatch.delenv(TEAMMATE_COMMAND_ENV_VAR, raising=False)
    monkeypatch.setattr(sys, "executable", "/tmp/current-python")

    command = get_teammate_command()

    assert command == "/tmp/current-python"


def test_build_inherited_env_vars_disables_coordinator_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_COORDINATOR_MODE", "1")

    env = build_inherited_env_vars()

    assert env["CLAUDE_CODE_COORDINATOR_MODE"] == "0"


def test_build_inherited_env_vars_forwards_seawall_config_dir(monkeypatch):
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", "/opt/data/.seawall")

    env = build_inherited_env_vars()

    assert env["SEAWALL_CONFIG_DIR"] == "/opt/data/.seawall"


def test_build_inherited_env_vars_includes_seawall_auth_vars(monkeypatch):
    monkeypatch.setenv("SEAWALL_PROVIDER", "openai")
    monkeypatch.setenv("SEAWALL_BASE_URL", "https://relay.example.com/v1")
    monkeypatch.setenv("SEAWALL_OPENAI_API_KEY", "sk-seawall-openai")
    monkeypatch.setenv("SEAWALL_ANTHROPIC_API_KEY", "sk-seawall-anthropic")

    env = build_inherited_env_vars()

    assert env["SEAWALL_AGENT_TEAMS"] == "1"
    assert env["SEAWALL_PROVIDER"] == "openai"
    assert env["SEAWALL_BASE_URL"] == "https://relay.example.com/v1"
    assert env["SEAWALL_OPENAI_API_KEY"] == "sk-seawall-openai"
    assert env["SEAWALL_ANTHROPIC_API_KEY"] == "sk-seawall-anthropic"


# ---------------------------------------------------------------------------
# build_inherited_cli_flags – model handling
# ---------------------------------------------------------------------------


def test_build_inherited_cli_flags_explicit_model_included():
    flags = build_inherited_cli_flags(model="claude-opus-4-5")
    assert "--model" in flags
    idx = flags.index("--model")
    assert "claude-opus-4-5" in flags[idx + 1]


def test_build_inherited_cli_flags_inherit_model_excluded():
    """model='inherit' must NOT produce a --model flag so the subprocess
    picks up the parent's model from the SEAWALL_MODEL env var."""
    flags = build_inherited_cli_flags(model="inherit")
    assert "--model" not in flags


def test_build_inherited_cli_flags_none_model_excluded():
    flags = build_inherited_cli_flags(model=None)
    assert "--model" not in flags


def test_build_inherited_cli_flags_empty_string_model_excluded():
    flags = build_inherited_cli_flags(model="")
    assert "--model" not in flags


def test_build_inherited_cli_flags_forwards_system_prompt_as_replace():
    flags = build_inherited_cli_flags(system_prompt="You are a specialized worker.")

    assert "--system-prompt" in flags
    idx = flags.index("--system-prompt")
    assert "specialized worker" in flags[idx + 1]
    assert "--append-system-prompt" not in flags


def test_build_inherited_cli_flags_forwards_system_prompt_as_append():
    flags = build_inherited_cli_flags(
        system_prompt="Extra worker instructions.",
        system_prompt_mode="append",
    )

    assert "--append-system-prompt" in flags
    idx = flags.index("--append-system-prompt")
    assert "Extra worker instructions." in flags[idx + 1]
    assert "--system-prompt" not in flags


# ---------------------------------------------------------------------------
# build_inherited_cli_flags – permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "plan", "full_auto"])
def test_build_inherited_cli_flags_passes_the_parent_mode_down(mode):
    flags = build_inherited_cli_flags(permission_mode=mode)
    assert flags == ["--permission-mode", mode]


def test_build_inherited_cli_flags_ignores_modes_it_does_not_know():
    assert build_inherited_cli_flags(permission_mode="bypassPermissions") == []
    assert build_inherited_cli_flags(permission_mode=None) == []


def test_build_inherited_cli_flags_plan_required_overrides_any_mode():
    flags = build_inherited_cli_flags(permission_mode="full_auto", plan_mode_required=True)
    assert flags == ["--permission-mode", "plan"]


def test_build_inherited_cli_flags_forwards_tool_lists():
    flags = build_inherited_cli_flags(
        permission_mode="default", allowed_tools=["bash", "glob"], disallowed_tools=["web_fetch"]
    )
    assert flags == [
        "--permission-mode", "default",
        "--allowed-tools", "bash,glob",
        "--disallowed-tools", "web_fetch",
    ]
