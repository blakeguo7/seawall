"""``seawall -p`` with budgets: stop reasons, usage in the output, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seawall.api.usage import UsageSnapshot
from seawall.config.settings import ModelPriceConfig, Settings, save_settings
from seawall.ui.app import run_print_mode
from seawall.ui.overrides import SessionOverrides
from tests.fakes import GeneratedClient, ScriptedClient, text_message, tool_message

ONE_CALL = UsageSnapshot(input_tokens=1000, output_tokens=0)


@pytest.fixture(autouse=True)
def priced_model():
    """Make every test model cost $1 per call, so a dollar budget is easy to hit."""
    save_settings(Settings(pricing={"claude-test": ModelPriceConfig(input=1000, output=0)}))


def endless() -> GeneratedClient:
    return GeneratedClient(
        lambda n: tool_message(("bash", {"command": f"echo step{n}"}), turn=n), usage=ONE_CALL
    )


async def run_json(tmp_path: Path, capsys, client, **kwargs) -> tuple[int, dict]:
    code = await run_print_mode(
        prompt="go",
        output_format="json",
        cwd=str(tmp_path),
        model="claude-test",
        api_client=client,
        permission_mode="full_auto",
        **kwargs,
    )
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


async def test_a_finished_run_reports_usage_and_cost(tmp_path: Path, capsys) -> None:
    code, result = await run_json(tmp_path, capsys, ScriptedClient(text_message("hi"), usage=ONE_CALL))

    assert code == 0
    assert result["stop_reason"] == "completed"
    assert result["turns"] == 1
    assert result["usage"]["input_tokens"] == 1000
    assert result["cost_usd"] == pytest.approx(1.0)
    assert result["unpriced_models"] == []
    assert result["text"] == "hi"


async def test_the_budget_flag_stops_the_run_with_exit_code_2(tmp_path: Path, capsys) -> None:
    client = endless()
    code, result = await run_json(
        tmp_path, capsys, client, overrides=SessionOverrides.from_cli(max_budget_usd=2.5)
    )

    assert code == 2
    assert result["stop_reason"] == "budget_exceeded"
    assert "Budget reached" in result["detail"]
    assert result["cost_usd"] == pytest.approx(3.0)
    assert len(client.requests) == 3


async def test_the_token_flag(tmp_path: Path, capsys) -> None:
    code, result = await run_json(
        tmp_path, capsys, endless(), overrides=SessionOverrides.from_cli(max_total_tokens=1500)
    )
    assert (code, result["stop_reason"]) == (2, "token_limit")


async def test_a_dollar_budget_for_an_unpriced_model_is_refused(tmp_path: Path, capsys) -> None:
    save_settings(Settings())  # no price for claude-test any more
    client = endless()
    code, result = await run_json(
        tmp_path, capsys, client, overrides=SessionOverrides.from_cli(max_budget_usd=5)
    )
    assert (code, result["stop_reason"]) == (2, "no_price")
    assert client.requests == []
    assert result["cost_usd"] == 0  # nothing was spent
    assert result["usage"]["input_tokens"] == 0


async def test_limits_from_settings_apply_without_flags(tmp_path: Path, capsys) -> None:
    save_settings(
        Settings(
            pricing={"claude-test": ModelPriceConfig(input=1000, output=0)},
            limits={"max_budget_usd": 1.5},
        )
    )
    code, result = await run_json(tmp_path, capsys, endless())
    assert (code, result["stop_reason"]) == (2, "budget_exceeded")


async def test_an_agent_going_in_circles_is_stopped(tmp_path: Path, capsys) -> None:
    client = GeneratedClient(
        lambda n: tool_message(("bash", {"command": "echo same"}), turn=n), usage=ONE_CALL
    )
    code, result = await run_json(tmp_path, capsys, client)
    assert (code, result["stop_reason"]) == (2, "loop_detected")
    assert len(client.requests) == 5


async def test_hitting_max_turns_exits_with_code_2(tmp_path: Path, capsys) -> None:
    code, result = await run_json(tmp_path, capsys, endless(), max_turns=2)
    assert (code, result["stop_reason"]) == (2, "max_turns")


async def test_text_mode_says_why_it_stopped(tmp_path: Path, capsys) -> None:
    code = await run_print_mode(
        prompt="go",
        cwd=str(tmp_path),
        model="claude-test",
        api_client=endless(),
        permission_mode="full_auto",
        overrides=SessionOverrides.from_cli(max_budget_usd=1.5),
    )
    assert code == 2
    assert "[stopped: budget_exceeded] Budget reached" in capsys.readouterr().err


async def test_stream_json_ends_with_a_run_finished_event(tmp_path: Path, capsys) -> None:
    await run_print_mode(
        prompt="go",
        output_format="stream-json",
        cwd=str(tmp_path),
        model="claude-test",
        api_client=ScriptedClient(text_message("hi"), usage=ONE_CALL),
    )
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last["type"] == "run_finished"
    assert last["stop_reason"] == "completed"
    assert last["cost_usd"] == pytest.approx(1.0)


async def test_api_errors_exit_with_code_1(tmp_path: Path, capsys) -> None:
    class Failing:
        async def stream_message(self, request):
            raise RuntimeError("provider is down")
            yield  # pragma: no cover

    code, result = await run_json(tmp_path, capsys, Failing())
    assert code == 1
    assert result["stop_reason"] == "error"
