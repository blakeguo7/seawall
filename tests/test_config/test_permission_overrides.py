"""Command-line overrides for permissions."""

from __future__ import annotations

from seawall.config.settings import PermissionSettings, Settings, normalize_tool_names
from seawall.permissions import PermissionMode


def test_tool_lists_are_split_on_commas_and_spaces() -> None:
    assert normalize_tool_names(["bash,edit_file", "glob  grep", ""]) == ["bash", "edit_file", "glob", "grep"]
    assert normalize_tool_names("a, b ,a") == ["a", "b"]
    assert normalize_tool_names(None) == []
    assert normalize_tool_names([]) == []


def test_overrides_add_to_the_configured_lists() -> None:
    base = Settings(permission=PermissionSettings(allowed_tools=["glob"], denied_tools=["web_fetch"]))
    merged = base.merge_cli_overrides(allowed_tools=["bash", "glob"], disallowed_tools="agent")

    assert merged.permission.allowed_tools == ["glob", "bash"]
    assert merged.permission.denied_tools == ["web_fetch", "agent"]
    assert base.permission.allowed_tools == ["glob"]  # the original is untouched


def test_overrides_can_be_combined_with_a_mode() -> None:
    merged = Settings().merge_cli_overrides(permission_mode="plan", allowed_tools=["glob"])
    assert merged.permission.mode is PermissionMode.PLAN
    assert merged.permission.allowed_tools == ["glob"]


def test_no_overrides_changes_nothing() -> None:
    base = Settings()
    merged = base.merge_cli_overrides(allowed_tools=None, disallowed_tools=[], permission_mode=None)
    assert merged.permission == base.permission


def test_append_system_prompt_is_a_setting() -> None:
    assert Settings().merge_cli_overrides(append_system_prompt="Be brief.").append_system_prompt == "Be brief."
