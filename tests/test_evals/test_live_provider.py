"""A live agent runs under a throwaway HOME, so what it needs from the user's setup must be handed over.

It defaults to the provider the user configured (model, endpoint, format, prices). The key follows
the destination: it is sent only where the user's own key belongs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seawall.auth.manager import AuthManager
from seawall.config.settings import ProviderProfile
from seawall.evals import runner
from seawall.evals.runner import AgentSpec, configured_key_env, use_configured_provider
from seawall.evals.task import load_task
from tests.test_evals.conftest import make_task

DEEPSEEK_URL = "https://api.deepseek.com/anthropic"
PRICES = {"deepseek-chat": {"input": 0.3, "output": 1.2, "cache_write": 0.3, "cache_read": 0.006}}


@pytest.fixture
def configured(_isolated_home: Path) -> AuthManager:
    """A DeepSeek profile with its own key and a price for its model, active in the isolated HOME."""
    manager = AuthManager()
    manager.upsert_profile(
        "deepseek",
        ProviderProfile(
            label="DeepSeek",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="deepseek-chat",
            base_url=DEEPSEEK_URL,
            credential_slot="deepseek",
        ),
    )
    manager.use_profile("deepseek")
    manager.store_profile_credential("deepseek", "api_key", "sk-deepseek")
    path = _isolated_home / ".seawall" / "settings.json"
    settings = json.loads(path.read_text())
    settings["pricing"] = PRICES
    path.write_text(json.dumps(settings))
    return manager


def test_a_live_agent_defaults_to_the_configured_provider(configured: AuthManager) -> None:
    spec = use_configured_provider(AgentSpec("live"))

    assert (spec.model, spec.base_url, spec.api_format) == ("deepseek-chat", DEEPSEEK_URL, "anthropic")
    assert spec.pricing == PRICES


def test_what_the_caller_gave_wins_over_the_configured_provider(configured: AuthManager) -> None:
    given = AgentSpec("live", model="my-model", base_url="https://gw.example/v1", api_format="openai")

    spec = use_configured_provider(given)

    assert (spec.model, spec.base_url, spec.api_format) == ("my-model", "https://gw.example/v1", "openai")


def test_the_configured_key_goes_only_to_the_configured_destination(configured: AuthManager) -> None:
    here = use_configured_provider(AgentSpec("live"))
    elsewhere = use_configured_provider(AgentSpec("live", base_url="https://gw.example/v1"))

    assert configured_key_env(here) == {"ANTHROPIC_API_KEY": "sk-deepseek"}
    assert configured_key_env(elsewhere) == {}


def test_a_key_already_in_the_environment_is_left_alone(configured: AuthManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    assert configured_key_env(use_configured_provider(AgentSpec("live"))) == {}


def test_no_stored_key_means_nothing_is_forwarded(_isolated_home: Path) -> None:
    assert configured_key_env(use_configured_provider(AgentSpec("live"))) == {}


async def test_the_prices_reach_the_agents_own_settings(
    configured: AuthManager, tasks_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a price in its own settings.json the agent refuses a dollar budget (`no_price`)."""
    seen: dict[str, dict] = {}

    async def fake_agent(*, home: Path, **kwargs):
        seen.update(json.loads((home / ".seawall" / "settings.json").read_text()))
        return runner.AgentProcessResult(exit_code=0, timed_out=False, stdout="", stderr="", result=None, duration_seconds=0.0)

    monkeypatch.setattr(runner, "run_agent_process", fake_agent)
    make_task(tasks_root)

    await runner.run_trial(
        load_task(tasks_root / "demo"), 1, use_configured_provider(AgentSpec("live")), out_dir=tmp_path / "runs", server=None
    )

    assert seen["pricing"] == PRICES
