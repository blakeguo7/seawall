"""Shared utilities for spawning sub-agent processes."""

from __future__ import annotations

import os
import shlex
import shutil
import sys


# Environment variable to override the sub-agent command
TEAMMATE_COMMAND_ENV_VAR = "SEAWALL_TEAMMATE_COMMAND"


# ---------------------------------------------------------------------------
# Environment variables forwarded to spawned sub-agents.
#
# Provider, proxy and config-directory settings that a sub-agent needs in order
# to reach the same endpoint as the parent, forwarded when they are set.
# ---------------------------------------------------------------------------

_TEAMMATE_ENV_VARS = [
    # --- API provider selection -------------------------------------------
    # Without these, sub-agents would default to the wrong endpoint provider
    # and fail all API calls.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    # --- Config directory override ----------------------------------------
    # Allows operator-level config to be visible inside sub-agent processes.
    "CLAUDE_CONFIG_DIR",
    # --- Remote / CCR markers ---------------------------------------------
    # CCR-aware code paths check CLAUDE_CODE_REMOTE.
    "CLAUDE_CODE_REMOTE",
    # Auto-memory gate checks REMOTE && !MEMORY_DIR to disable memory on
    # ephemeral CCR filesystems.  Forwarding REMOTE alone would flip
    # sub-agents to memory-off when the parent has it on.
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    # --- Upstream proxy settings ------------------------------------------
    # Forward proxy vars so sub-agents route traffic the same way as the
    # parent; without these they would bypass the configured proxy entirely.
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    # --- CA bundle overrides ----------------------------------------------
    # Custom CA certificates must be visible to sub-agents when TLS inspection
    # is in use; missing these causes SSL verification failures.
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    # --- Seawall-native provider settings ---------------------------------
    # These are read by settings._apply_env_overrides() so sub-agents use the
    # same provider as the parent.
    "SEAWALL_CONFIG_DIR",
    "SEAWALL_DATA_DIR",
    "SEAWALL_LOGS_DIR",
    "SEAWALL_PROFILE",
    "SEAWALL_API_FORMAT",
    "SEAWALL_PROVIDER",
    "SEAWALL_BASE_URL",
    "SEAWALL_MODEL",
    "SEAWALL_ANTHROPIC_API_KEY",
    "SEAWALL_OPENAI_API_KEY",
    "SEAWALL_DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
]


def get_teammate_command() -> str:
    """Return the executable used to spawn sub-agent processes.

    Resolution order:
    1. ``SEAWALL_TEAMMATE_COMMAND`` environment variable — allows the
       operator to point at a specific binary or wrapper script.
    2. The current Python interpreter running the ``seawall`` module.
       This keeps spawned sub-agents on the same venv/source tree as the
       parent process.
    3. The ``seawall`` entry-point on PATH (installed package fallback).
    """
    override = os.environ.get(TEAMMATE_COMMAND_ENV_VAR)
    if override:
        return override

    # Prefer the current interpreter so sub-agents inherit the same runtime and
    # editable-install source tree as the parent process.
    if sys.executable:
        return sys.executable

    entry_point = shutil.which("seawall")
    if entry_point:
        return entry_point
    return "python"


def build_inherited_cli_flags(
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    system_prompt_mode: str | None = None,
    permission_mode: str | None = None,
    plan_mode_required: bool = False,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    settings_path: str | None = None,
    plugin_dirs: list[str] | None = None,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build CLI flags to propagate from the current session to spawned sub-agents.

    Ensures sub-agents inherit important settings like permission mode, model
    selection, and plugin configuration from their parent.

    All flag values are shell-quoted with :func:`shlex.quote` to prevent
    command injection when the resulting list is later joined into a shell
    command string.

    Args:
        model: Model override to forward (e.g. ``"claude-opus-4-6"``).
        system_prompt: System prompt override to forward to the sub-agent.
        system_prompt_mode: One of ``"replace"``/``"default"`` or ``"append"``.
            ``append`` maps to ``--append-system-prompt``; anything else uses
            ``--system-prompt``.
        permission_mode: The parent's mode (``default``, ``plan`` or ``full_auto``); the
            sub-agent runs under the same mode. Anything else is ignored.
        plan_mode_required: When True the sub-agent runs in plan mode whatever the
            parent's mode is.
        allowed_tools: Tools the parent allows without asking, inherited by the sub-agent.
        disallowed_tools: Tools the parent denies, inherited by the sub-agent.
        settings_path: Path to a settings JSON file to propagate via
            ``--settings``.  Shell-quoted for safety.
        plugin_dirs: List of plugin directory paths.  Each is forwarded as a
            separate ``--plugin-dir <path>`` flag so inline plugins are
            visible inside sub-agent processes.
        extra_flags: Additional pre-built flag strings to append verbatim.
            Callers are responsible for quoting any values in these strings.

    Returns:
        List of CLI flag strings ready to be passed to :mod:`subprocess`.
    """
    flags: list[str] = []

    # --- Permissions -------------------------------------------------------
    # A sub-agent never gets more latitude than its parent. It cannot ask anyone for
    # approval, so under "default" its mutating calls are refused rather than waved through.
    if plan_mode_required:
        flags.extend(["--permission-mode", "plan"])
    elif permission_mode in {"default", "plan", "full_auto"}:
        flags.extend(["--permission-mode", permission_mode])
    if allowed_tools:
        flags.extend(["--allowed-tools", shlex.quote(",".join(allowed_tools))])
    if disallowed_tools:
        flags.extend(["--disallowed-tools", shlex.quote(",".join(disallowed_tools))])

    # --- Model override ----------------------------------------------------
    # "inherit" means use the parent's model via the SEAWALL_MODEL env var.
    if model and model != "inherit":
        flags.extend(["--model", shlex.quote(model)])

    # --- System prompt override ------------------------------------------
    # Agent definitions can carry a dedicated worker system prompt. Forward it
    # explicitly so sub-agents preserve their role.
    if system_prompt:
        prompt_flag = "--append-system-prompt" if system_prompt_mode == "append" else "--system-prompt"
        flags.extend([prompt_flag, shlex.quote(system_prompt)])

    # --- Settings path propagation ----------------------------------------
    # Ensures sub-agents load the same settings JSON as the parent process.
    if settings_path:
        flags.extend(["--settings", shlex.quote(settings_path)])

    # --- Plugin directories -----------------------------------------------
    # Each enabled plugin directory is forwarded individually so that inline
    # plugins (loaded via --plugin-dir) are available inside sub-agents.
    for plugin_dir in plugin_dirs or []:
        flags.extend(["--plugin-dir", shlex.quote(plugin_dir)])

    if extra_flags:
        flags.extend(extra_flags)

    return flags


def build_inherited_env_vars() -> dict[str, str]:
    """Build environment variables to forward to spawned sub-agents.

    Always includes ``SEAWALL_AGENT_TEAMS=1`` plus any provider/proxy
    vars that are set in the current process.

    Returns:
        Dict of env var name → value to merge into the subprocess environment.
    """
    env: dict[str, str] = {
        "SEAWALL_AGENT_TEAMS": "1",
        # Spawned workers should behave like workers, not recursively re-enter
        # coordinator mode just because the parent had the flag set.
        "CLAUDE_CODE_COORDINATOR_MODE": "0",
    }

    for key in _TEAMMATE_ENV_VARS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    return env
