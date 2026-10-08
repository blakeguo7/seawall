"""CLI smoke tests."""

import json
import re
import sys
import types
from pathlib import Path

from typer.testing import CliRunner

import seawall.cli as cli
from seawall.config import load_settings
from seawall.config.settings import Settings
from seawall.mcp.types import McpStdioServerConfig


app = cli.app


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--help"],
        env={"NO_COLOR": "1", "COLUMNS": "160"},
    )
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 0
    assert "Seawall: a coding-agent harness" in plain_output
    assert "setup" in plain_output
    assert "--dry-run" in plain_output


def test_setup_flow_prompts_for_missing_api_key_and_sets_model(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SEAWALL_OPENAI_API_KEY", raising=False)

    from seawall.auth.storage import load_credential

    selections = iter(["openai-compatible", "openai-compatible"])
    monkeypatch.setattr("seawall.cli._select_setup_workflow", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._select_from_menu", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._prompt_model_for_profile", lambda profile: "gpt-5.4")
    monkeypatch.setattr("seawall.auth.flows.ApiKeyFlow.run", lambda self: "sk-test")

    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "Setup complete:" in result.output

    settings = load_settings()
    assert settings.active_profile == "openai-compatible"
    assert settings.resolve_profile()[1].last_model == "gpt-5.4"
    assert load_credential("openai", "api_key") == "sk-test"


def test_select_from_menu_uses_questionary_when_tty(monkeypatch):
    answers = []

    class _Prompt:
        def ask(self):
            return "openai-compatible"

    fake_questionary = types.SimpleNamespace(
        Choice=lambda title, value, checked=False: {
            "title": title,
            "value": value,
            "checked": checked,
        },
        select=lambda title, choices, default=None: answers.append((title, choices, default)) or _Prompt(),
    )

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys, "__stdin__", sys.stdin)
    monkeypatch.setattr(cli.sys, "__stdout__", sys.stdout)
    monkeypatch.setitem(sys.modules, "questionary", fake_questionary)

    result = cli._select_from_menu(
        "Choose a provider workflow:",
        [("openai-compatible", "OpenAI API"), ("claude-api", "Claude API")],
        default_value="openai-compatible",
    )

    assert result == "openai-compatible"
    assert answers


def test_setup_flow_existing_api_key_profile_can_update_secret(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from seawall.auth.manager import AuthManager
    from seawall.auth.storage import load_credential

    manager = AuthManager()
    manager.store_profile_credential("openai-compatible", "api_key", "old-key")

    selections = iter(["openai-compatible", "openai-compatible"])
    monkeypatch.setattr("seawall.cli._select_setup_workflow", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._select_from_menu", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._confirm_prompt", lambda *args, **kwargs: True)
    monkeypatch.setattr("seawall.auth.flows.ApiKeyFlow.run", lambda self: "new-key")
    monkeypatch.setattr("seawall.cli._prompt_model_for_profile", lambda profile: "gpt-4.1")

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "Setup complete:" in result.output
    assert load_credential("openai", "api_key") == "new-key"


def test_setup_flow_creates_deepseek_profile_with_profile_scoped_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path))
    # Prevent env var leakage from overriding the configured api_key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    selections = iter(["claude-api", "deepseek-anthropic"])
    prompts = iter(
        [
            "https://api.deepseek.com/anthropic",
            "deepseek-chat",
        ]
    )

    monkeypatch.setattr("seawall.cli._select_setup_workflow", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._select_from_menu", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("seawall.cli._text_prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr("seawall.auth.flows.ApiKeyFlow.run", lambda self: "sk-deepseek-test")

    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "Setup complete:" in result.output
    assert "- profile: deepseek-anthropic" in result.output

    settings = load_settings()
    assert settings.active_profile == "deepseek-anthropic"
    profile = settings.resolve_profile()[1]
    assert profile.base_url == "https://api.deepseek.com/anthropic"
    assert profile.credential_slot == "deepseek-anthropic"
    assert profile.allowed_models == ["deepseek-chat"]

    from seawall.auth.storage import load_credential

    assert load_credential("profile:deepseek-anthropic", "api_key") == "sk-deepseek-test"


def test_provider_add_can_store_profile_api_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path))

    from seawall.auth.storage import load_credential

    result = runner.invoke(
        app,
        [
            "provider",
            "add",
            "custom-openai",
            "--label",
            "Custom OpenAI",
            "--provider",
            "openai",
            "--api-format",
            "openai",
            "--auth-source",
            "openai_api_key",
            "--model",
            "gpt-4.1",
            "--credential-slot",
            "custom-openai",
            "--api-key",
            "new-key",
        ],
    )

    assert result.exit_code == 0
    assert "API key set" in result.output
    assert load_credential("profile:custom-openai", "api_key") == "new-key"


def test_provider_edit_can_replace_profile_api_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("SEAWALL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from seawall.auth.manager import AuthManager
    from seawall.auth.storage import load_credential

    manager = AuthManager()
    manager.store_profile_credential("openai-compatible", "api_key", "old-key")

    result = runner.invoke(app, ["provider", "edit", "openai-compatible", "--api-key", "new-key"])

    assert result.exit_code == 0
    assert "API key replaced" in result.output
    assert load_credential("openai", "api_key") == "new-key"


def test_dangerously_skip_permissions_passes_full_auto_to_run_repl(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def fake_run_repl(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("seawall.ui.app.run_repl", fake_run_repl)

    result = runner.invoke(app, ["--dangerously-skip-permissions"])

    assert result.exit_code == 0
    assert captured["permission_mode"] == "full_auto"


def test_task_worker_flag_routes_to_run_task_worker(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def fake_run_task_worker(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("seawall.ui.app.run_task_worker", fake_run_task_worker)

    result = runner.invoke(app, ["--task-worker", "--model", "deepseek-chat"])

    assert result.exit_code == 0
    assert captured["model"] == "deepseek-chat"


def test_dry_run_uses_preview_builder_and_skips_repl(monkeypatch):
    runner = CliRunner()
    captured = {}

    def fake_build_dry_run_preview(**kwargs):
        captured.update(kwargs)
        return {
            "cwd": kwargs["cwd"],
            "prompt_preview": kwargs["prompt"],
            "settings": {
                "active_profile": "claude-api",
                "profile_label": "Claude API",
                "provider": "anthropic",
                "api_format": "anthropic",
                "model": "claude-sonnet-4-6",
                "base_url": "",
                "permission_mode": "default",
                "max_turns": 200,
                "effort": "medium",
                "passes": 1,
            },
            "validation": {
                "auth_status": "configured",
                "api_client": {"status": "ok"},
                "system_prompt_chars": 123,
                "mcp_validation": "skipped",
            },
            "entrypoint": {"kind": "model_prompt", "detail": "preview only"},
            "plugins": [],
            "skills": [],
            "commands": [],
            "tools": [],
            "mcp_servers": [],
            "system_prompt_preview": "preview",
        }

    async def fake_run_repl(**kwargs):  # pragma: no cover - should never be called
        raise AssertionError(f"run_repl should not be called during dry-run: {kwargs}")

    monkeypatch.setattr("seawall.cli._build_dry_run_preview", fake_build_dry_run_preview)
    monkeypatch.setattr("seawall.ui.app.run_repl", fake_run_repl)

    result = runner.invoke(app, ["--dry-run", "--print", "ship it", "--model", "gpt-5.4"])

    assert result.exit_code == 0
    assert captured["prompt"] == "ship it"
    assert captured["model"] == "gpt-5.4"
    assert "Seawall Dry Run" in result.output
    assert "ship it" in result.output


def test_dry_run_json_output(monkeypatch):
    runner = CliRunner()

    def fake_build_dry_run_preview(**kwargs):
        return {
            "mode": "dry-run",
            "cwd": kwargs["cwd"],
            "prompt": kwargs["prompt"],
            "prompt_preview": kwargs["prompt"],
            "settings": {
                "active_profile": "claude-api",
                "profile_label": "Claude API",
                "provider": "anthropic",
                "api_format": "anthropic",
                "model": "claude-sonnet-4-6",
                "base_url": "",
                "permission_mode": "default",
                "max_turns": 200,
                "effort": "medium",
                "passes": 1,
            },
            "validation": {
                "auth_status": "configured",
                "api_client": {"status": "ok"},
                "system_prompt_chars": 123,
                "mcp_validation": "skipped",
            },
            "entrypoint": {"kind": "interactive_session", "detail": "wait"},
            "plugins": [],
            "skills": [],
            "commands": [],
            "tools": [],
            "mcp_servers": [],
            "system_prompt_preview": "preview",
        }

    monkeypatch.setattr("seawall.cli._build_dry_run_preview", fake_build_dry_run_preview)

    result = runner.invoke(app, ["--dry-run", "--output-format", "json", "--print", "preview this"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert payload["prompt"] == "preview this"


def test_dry_run_rejects_continue_resume(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("seawall.cli._build_dry_run_preview", lambda **kwargs: {"mode": "dry-run"})

    result = runner.invoke(app, ["--dry-run", "--continue"])

    assert result.exit_code == 1
    assert "--dry-run does not support --continue/--resume yet" in result.output


def test_build_dry_run_preview_classifies_slash_command_and_flags_bad_mcp(monkeypatch, tmp_path: Path):
    settings = Settings(
        api_key="sk-test",
        mcp_servers={
            "broken": McpStdioServerConfig(command="definitely-not-a-real-command-seawall"),
        },
    )

    class _FakeSkillRegistry:
        def list_skills(self):
            return []

    monkeypatch.setattr("seawall.config.load_settings", lambda: settings)
    monkeypatch.setattr(
        "seawall.api.provider.detect_provider",
        lambda settings: types.SimpleNamespace(name="anthropic"),
    )
    monkeypatch.setattr("seawall.api.provider.auth_status", lambda settings: "configured")
    monkeypatch.setattr("seawall.plugins.load_plugins", lambda settings, cwd: [])
    monkeypatch.setattr("seawall.skills.load_skill_registry", lambda cwd, settings=None: _FakeSkillRegistry())
    monkeypatch.setattr("seawall.prompts.context.build_runtime_system_prompt", lambda *args, **kwargs: "preview prompt")
    monkeypatch.setattr("seawall.ui.runtime._resolve_api_client_from_settings", lambda settings: object())

    preview = cli._build_dry_run_preview(
        prompt="/plugin list",
        cwd=str(tmp_path),
        model=None,
        max_turns=None,
        base_url=None,
        system_prompt=None,
        api_key=None,
        api_format=None,
        permission_mode=None,
    )

    assert preview["entrypoint"]["kind"] == "slash_command"
    assert preview["entrypoint"]["command"] == "plugin"
    assert preview["entrypoint"]["behavior"] == "stateful"
    assert preview["validation"]["mcp_errors"] == 1
    assert preview["mcp_servers"][0]["status"] == "error"
    assert "command not found in PATH" in preview["mcp_servers"][0]["issues"][0]


def test_build_dry_run_preview_sets_blocked_when_model_prompt_lacks_auth(monkeypatch, tmp_path: Path):
    settings = Settings(api_key="")

    class _FakeSkillRegistry:
        def list_skills(self):
            return []

    monkeypatch.setattr("seawall.config.load_settings", lambda: settings)
    monkeypatch.setattr(
        "seawall.api.provider.detect_provider",
        lambda settings: types.SimpleNamespace(name="anthropic"),
    )
    monkeypatch.setattr("seawall.api.provider.auth_status", lambda settings: "missing")
    monkeypatch.setattr("seawall.plugins.load_plugins", lambda settings, cwd: [])
    monkeypatch.setattr("seawall.skills.load_skill_registry", lambda cwd, settings=None: _FakeSkillRegistry())
    monkeypatch.setattr("seawall.prompts.context.build_runtime_system_prompt", lambda *args, **kwargs: "preview prompt")

    def fake_resolve_api_client(settings):
        raise SystemExit(1)

    monkeypatch.setattr("seawall.ui.runtime._resolve_api_client_from_settings", fake_resolve_api_client)

    preview = cli._build_dry_run_preview(
        prompt="fix the failing tests",
        cwd=str(tmp_path),
        model=None,
        max_turns=None,
        base_url=None,
        system_prompt=None,
        api_key=None,
        api_format=None,
        permission_mode=None,
    )

    assert preview["entrypoint"]["kind"] == "model_prompt"
    assert preview["readiness"]["level"] == "blocked"
    assert any("runtime client" in reason.lower() for reason in preview["readiness"]["reasons"])
    assert any("authentication" in action.lower() or "profile" in action.lower() for action in preview["readiness"]["next_actions"])


def test_build_dry_run_preview_recommends_matching_skills_and_tools(monkeypatch, tmp_path: Path):
    settings = Settings(api_key="sk-test")

    class _FakeSkillRegistry:
        def list_skills(self):
            return [
                types.SimpleNamespace(
                    name="review",
                    description="Review code for bugs and regressions.",
                    content="Use this when reviewing bug fixes and regressions.",
                    source="bundled",
                ),
                types.SimpleNamespace(
                    name="plan",
                    description="Plan implementation work before coding.",
                    content="Use this to design an implementation plan.",
                    source="bundled",
                ),
            ]

    class _FakeToolRegistry:
        def to_api_schema(self):
            return [
                {
                    "name": "grep",
                    "description": "Search code for bug patterns and failing lines.",
                    "input_schema": {"properties": {"pattern": {}, "root": {}}, "required": ["pattern"]},
                },
                {
                    "name": "read_file",
                    "description": "Read files from disk.",
                    "input_schema": {"properties": {"path": {}, "offset": {}}, "required": ["path"]},
                },
            ]

    monkeypatch.setattr("seawall.config.load_settings", lambda: settings)
    monkeypatch.setattr(
        "seawall.api.provider.detect_provider",
        lambda settings: types.SimpleNamespace(name="anthropic"),
    )
    monkeypatch.setattr("seawall.api.provider.auth_status", lambda settings: "configured")
    monkeypatch.setattr("seawall.plugins.load_plugins", lambda settings, cwd: [])
    monkeypatch.setattr("seawall.skills.load_skill_registry", lambda cwd, settings=None: _FakeSkillRegistry())
    monkeypatch.setattr("seawall.tools.create_default_tool_registry", lambda: _FakeToolRegistry())
    monkeypatch.setattr("seawall.prompts.context.build_runtime_system_prompt", lambda *args, **kwargs: "preview prompt")
    monkeypatch.setattr("seawall.ui.runtime._resolve_api_client_from_settings", lambda settings: object())

    preview = cli._build_dry_run_preview(
        prompt="review this bug fix and grep for failing tests",
        cwd=str(tmp_path),
        model=None,
        max_turns=None,
        base_url=None,
        system_prompt=None,
        api_key=None,
        api_format=None,
        permission_mode=None,
    )

    recommended_skills = [entry["name"] for entry in preview["recommendations"]["skills"]]
    recommended_tools = [entry["name"] for entry in preview["recommendations"]["tools"]]

    assert preview["readiness"]["level"] == "ready"
    assert any("you can run this prompt directly" in action.lower() for action in preview["readiness"]["next_actions"])
    assert "review" in recommended_skills
    assert "grep" in recommended_tools

