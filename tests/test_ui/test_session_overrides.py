"""Command-line overrides that travel to the backend host and the runtime."""

from __future__ import annotations

from seawall.config.settings import Settings
from seawall.ui.overrides import SessionOverrides
from seawall.ui.react_launcher import build_backend_command


def test_from_cli_normalises_tool_lists() -> None:
    overrides = SessionOverrides.from_cli(
        allowed_tools=["bash, edit_file", "glob"], disallowed_tools="web_fetch agent"
    )
    assert overrides.allowed_tools == ("bash", "edit_file", "glob")
    assert overrides.disallowed_tools == ("web_fetch", "agent")


def test_nothing_set_means_nothing_overridden() -> None:
    overrides = SessionOverrides.from_cli()
    assert overrides.to_cli_args() == []
    assert all(value is None for value in overrides.settings_overrides().values())
    assert Settings().merge_cli_overrides(**overrides.settings_overrides()) == Settings()


def test_every_option_survives_the_round_trip_to_a_child_process() -> None:
    overrides = SessionOverrides.from_cli(
        allowed_tools=["bash"],
        disallowed_tools=["agent"],
        append_system_prompt="Be brief.",
        max_budget_usd=2.5,
        max_total_tokens=100_000,
        max_seconds=90,
    )
    assert overrides.to_cli_args() == [
        "--allowed-tools", "bash",
        "--disallowed-tools", "agent",
        "--append-system-prompt", "Be brief.",
        "--max-budget-usd", "2.5",
        "--max-total-tokens", "100000",
        "--max-seconds", "90",
    ]


def test_overrides_reach_the_effective_settings() -> None:
    overrides = SessionOverrides.from_cli(
        allowed_tools=["bash"], max_budget_usd=2.5, max_total_tokens=1000, max_seconds=30
    )
    merged = Settings().merge_cli_overrides(**overrides.settings_overrides())

    assert merged.permission.allowed_tools == ["bash"]
    assert (merged.limits.max_budget_usd, merged.limits.max_total_tokens, merged.limits.max_seconds) == (
        2.5,
        1000,
        30,
    )


def test_the_tui_backend_is_started_with_the_same_overrides() -> None:
    command = build_backend_command(
        permission_mode="plan", overrides=SessionOverrides.from_cli(max_budget_usd=1.0, allowed_tools=["glob"])
    )
    assert command[-6:] == [
        "--permission-mode", "plan", "--allowed-tools", "glob", "--max-budget-usd", "1.0",
    ]
