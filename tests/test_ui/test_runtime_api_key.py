"""Tests for build_runtime auth failure handling."""

from __future__ import annotations

import pytest

from seawall.ui.runtime import build_runtime


@pytest.mark.asyncio
async def test_build_runtime_exits_cleanly_when_auth_resolution_fails(monkeypatch):
    """build_runtime should raise SystemExit(1) — not ValueError — when auth resolution fails."""

    def fake_resolve_auth(self):
        raise ValueError("No credentials found")

    monkeypatch.setattr("seawall.config.settings.Settings.resolve_auth", fake_resolve_auth)

    with pytest.raises(SystemExit, match="1"):
        await build_runtime(active_profile="claude-api")


@pytest.mark.asyncio
async def test_build_runtime_exits_cleanly_for_openai_format(monkeypatch):
    """Same check for the openai-compatible path."""

    def fake_resolve_auth(self):
        raise ValueError("No credentials found")

    monkeypatch.setattr("seawall.config.settings.Settings.resolve_auth", fake_resolve_auth)

    with pytest.raises(SystemExit, match="1"):
        await build_runtime(active_profile="openai-compatible", api_format="openai")


@pytest.mark.asyncio
async def test_build_runtime_explains_how_to_configure_an_api_key(monkeypatch, tmp_path, capsys):
    """A missing API key should tell the user how to configure one."""
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("ANTHROPIC_API_KEY", "SEAWALL_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit, match="1"):
        await build_runtime(active_profile="claude-api")

    captured = capsys.readouterr()
    assert "No API key configured" in captured.err
    assert "seawall auth login" in captured.err
