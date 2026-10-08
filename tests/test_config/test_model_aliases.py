"""Claude's tier names (sonnet, opus, haiku, best, opusplan) only mean something for a profile that serves Claude.

On an Anthropic-compatible endpoint that serves other models (DeepSeek, a gateway), a Claude model
name is not refused: the endpoint answers with a model of its own, and the cost is estimated at
Claude's prices. So there the tier names stand for the profile's own model.
"""

from __future__ import annotations

import pytest

from seawall.config.settings import ProviderProfile, Settings, resolve_model_setting

TIERS = ["sonnet", "opus", "haiku", "best", "opusplan", "sonnet[1m]", "opus[1m]", "Sonnet"]


def deepseek_profile(**extra) -> ProviderProfile:
    return ProviderProfile(
        label="DeepSeek",
        provider="anthropic",
        api_format="anthropic",
        auth_source="anthropic_api_key",
        default_model="deepseek-flash",
        base_url="https://api.deepseek.com/anthropic",
        **extra,
    )


@pytest.mark.parametrize("alias", TIERS)
def test_a_profile_that_serves_another_model_has_no_claude_tiers(alias: str) -> None:
    assert resolve_model_setting(alias, "anthropic", default_model="deepseek-flash") == "deepseek-flash"


@pytest.mark.parametrize("own_model", [None, "", "default", "claude-sonnet-4-6", "anthropic/claude-opus-4-6", "sonnet", "opus"])
def test_a_profile_that_serves_claude_keeps_the_tiers(own_model: str | None) -> None:
    assert resolve_model_setting("sonnet", "anthropic", default_model=own_model) == "claude-sonnet-4-6"
    assert resolve_model_setting("opus", "anthropic", default_model=own_model) == "claude-opus-4-6"
    assert resolve_model_setting("haiku", "anthropic", default_model=own_model) == "claude-haiku-4-5"
    assert resolve_model_setting("best", "anthropic", default_model=own_model) == "claude-opus-4-6"
    assert resolve_model_setting("opusplan", "anthropic", default_model=own_model, permission_mode="plan") == "claude-opus-4-6"
    assert resolve_model_setting("opusplan", "anthropic", default_model=own_model, permission_mode="default") == "claude-sonnet-4-6"


def test_a_model_name_written_in_full_is_the_users_choice_and_is_not_touched() -> None:
    assert resolve_model_setting("claude-sonnet-4-6", "anthropic", default_model="deepseek-flash") == "claude-sonnet-4-6"
    assert resolve_model_setting("deepseek-v4-pro", "anthropic", default_model="deepseek-flash") == "deepseek-v4-pro"


def test_default_still_means_the_profiles_own_model() -> None:
    assert resolve_model_setting("default", "anthropic", default_model="deepseek-flash") == "deepseek-flash"
    assert resolve_model_setting("", "anthropic", default_model="deepseek-flash") == "deepseek-flash"


def test_a_tier_chosen_in_the_profile_resolves_to_its_own_model() -> None:
    assert deepseek_profile(last_model="sonnet").resolved_model == "deepseek-flash"
    assert deepseek_profile(last_model="deepseek-v4-pro").resolved_model == "deepseek-v4-pro"


@pytest.mark.parametrize("alias", ["sonnet", "haiku", "opus", "best"])
def test_the_model_flag_on_a_deepseek_profile_ends_up_on_deepseek_flash(alias: str) -> None:
    """The path `seawall --model sonnet` takes, and the one a sub-agent's model argument takes."""
    settings = Settings(active_profile="deepseek", profiles={"deepseek": deepseek_profile()})

    assert settings.merge_cli_overrides(model=alias).model == "deepseek-flash"


def test_the_built_in_claude_profile_is_unchanged() -> None:
    settings = Settings(active_profile="claude-api")

    assert settings.merge_cli_overrides(model="sonnet").model == "claude-sonnet-4-6"
    assert settings.merge_cli_overrides(model="haiku").model == "claude-haiku-4-5"
