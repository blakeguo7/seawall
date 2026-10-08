"""A profile with its own credential slot must not lend its key to other profiles.

``Settings.api_key`` is one flat field. If a slot profile's key is copied into it, every
profile that has no key of its own reads that key as theirs: the status table reports them as
ready, and switching to one sends the key to the wrong provider.
"""

import pytest

from seawall.auth.manager import AuthManager
from seawall.auth.storage import load_credential
from seawall.config import load_settings
from seawall.config.settings import ProviderProfile


@pytest.fixture
def manager() -> AuthManager:
    """An active DeepSeek profile bound to its own credential slot, next to the built-in claude-api."""
    manager = AuthManager()
    manager.upsert_profile(
        "deepseek",
        ProviderProfile(
            label="DeepSeek",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="deepseek-chat",
            base_url="https://api.deepseek.com/anthropic",
            credential_slot="deepseek",
        ),
    )
    manager.use_profile("deepseek")
    return manager


def test_slot_profile_key_is_not_copied_into_flat_settings(manager: AuthManager):
    manager.store_profile_credential("deepseek", "api_key", "sk-deepseek")

    assert load_credential("profile:deepseek", "api_key") == "sk-deepseek"
    assert load_settings().api_key == ""


def test_shared_provider_key_is_not_copied_while_a_slot_profile_is_active(manager: AuthManager):
    manager.store_credential("anthropic", "api_key", "sk-claude")

    assert load_credential("anthropic", "api_key") == "sk-claude"
    assert load_settings().api_key == ""


def test_profile_without_its_own_key_is_not_reported_ready(manager: AuthManager):
    manager.store_profile_credential("deepseek", "api_key", "sk-deepseek")

    statuses = AuthManager().get_profile_statuses()

    assert statuses["deepseek"]["configured"] is True
    assert statuses["claude-api"]["configured"] is False


def test_slot_profile_is_not_ready_with_only_the_shared_provider_key(manager: AuthManager):
    """The key stored for claude-api is not the one DeepSeek will read, so it must not count."""
    manager.store_credential("anthropic", "api_key", "sk-claude")

    statuses = AuthManager().get_profile_statuses()

    assert statuses["claude-api"]["configured"] is True
    assert statuses["deepseek"]["configured"] is False


def test_switching_profiles_does_not_send_the_slot_key_elsewhere(manager: AuthManager):
    manager.store_profile_credential("deepseek", "api_key", "sk-deepseek")
    manager.use_profile("claude-api")

    with pytest.raises(ValueError, match="No credentials found"):
        load_settings().resolve_auth()


def test_environment_key_still_counts_for_every_profile_on_that_source(
    manager: AuthManager, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    statuses = AuthManager().get_profile_statuses()

    assert statuses["deepseek"]["configured"] is True
    assert statuses["claude-api"]["configured"] is True
