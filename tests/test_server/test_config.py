from __future__ import annotations

from dataclasses import replace

import pytest

from seawall.server.config import ConfigError, ServerConfig, is_loopback


@pytest.fixture
def base(tmp_path):
    return ServerConfig(workspace_root=tmp_path, db_path=tmp_path / "db", token="x" * 20)


def test_a_sound_configuration_validates(base):
    base.validate()


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True), ("127.5.5.5", True),
    ("0.0.0.0", False), ("192.168.1.5", False), ("example.com", False), ("", False),
])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_the_workspace_must_exist(base, tmp_path):
    with pytest.raises(ConfigError, match="not a directory"):
        replace(base, workspace_root=tmp_path / "nope").validate()


def test_a_token_is_required_unless_explicitly_waived_on_loopback(base):
    with pytest.raises(ConfigError, match="no token"):
        replace(base, token=None).validate()
    replace(base, token=None, allow_no_auth=True).validate()
    with pytest.raises(ConfigError, match="loopback"):
        replace(base, token=None, allow_no_auth=True, host="0.0.0.0").validate()
    with pytest.raises(ConfigError, match="loopback"):
        replace(base, token=None, allow_no_auth=True, host="").validate()


def test_a_short_token_is_refused(base):
    with pytest.raises(ConfigError, match="16 characters"):
        replace(base, token="short").validate()


@pytest.mark.parametrize("field", [
    "max_concurrent_runs", "max_loaded_sessions", "max_sessions", "max_message_chars", "max_pending_events",
])
def test_counts_must_be_positive(base, field):
    with pytest.raises(ConfigError, match=field):
        replace(base, **{field: 0}).validate()


def test_the_cache_must_hold_every_running_session(base):
    with pytest.raises(ConfigError, match="max_loaded_sessions"):
        replace(base, max_concurrent_runs=8, max_loaded_sessions=4).validate()


def test_the_queue_may_be_zero_but_not_negative(base):
    replace(base, max_queued_runs=0).validate()
    with pytest.raises(ConfigError):
        replace(base, max_queued_runs=-1).validate()


@pytest.mark.parametrize("field", [
    "lease_seconds", "approval_timeout_seconds", "run_timeout_seconds", "poll_seconds", "heartbeat_seconds",
    "max_budget_usd", "max_seconds",
])
def test_durations_and_caps_must_be_positive(base, field):
    with pytest.raises(ConfigError, match=field):
        replace(base, **{field: 0}).validate()


def test_the_body_limit_follows_the_message_limit(base):
    assert replace(base, max_message_chars=1000).max_body_bytes > 6000
