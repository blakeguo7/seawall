"""The ``seawall serve`` command."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

# Only light imports at module level: this file is loaded by every `seawall` invocation to register the
# command. The web stack (starlette, uvicorn) is imported when the server actually starts.
from seawall.server.config import TOKEN_ENV, ConfigError, ServerConfig


def serve(
    workspace: Path = typer.Option(
        ..., "--workspace", help="Directory sessions may work in; a session's cwd must be inside it"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Address to listen on"),
    port: int = typer.Option(8765, "--port", help="Port to listen on"),
    db: Path | None = typer.Option(None, "--db", help="SQLite file for sessions and events (default: <data dir>/server/sessions.db)"),
    token_env: str = typer.Option(TOKEN_ENV, "--token-env", help="Environment variable holding the bearer token (at least 16 characters)"),
    insecure_no_auth: bool = typer.Option(False, "--insecure-no-auth", help="Serve without a token. Only on a loopback address"),
    allow_full_auto: bool = typer.Option(False, "--allow-full-auto", help="Let clients start sessions that run tools without approval (full_auto, or allowed_tools)"),
    max_concurrent_runs: int = typer.Option(4, "--max-concurrent-runs", help="Runs executing at once"),
    max_queued_runs: int = typer.Option(16, "--max-queued-runs", help="Runs allowed to wait for a free slot; more get HTTP 429"),
    max_loaded_sessions: int = typer.Option(16, "--max-loaded-sessions", help="Session runtimes kept in memory"),
    max_sessions: int = typer.Option(1000, "--max-sessions", help="Sessions the server will store"),
    max_budget_usd: float | None = typer.Option(None, "--max-budget-usd", help="Cap on a session's cost; also its default"),
    max_total_tokens: int | None = typer.Option(None, "--max-total-tokens", help="Cap on a session's tokens; also its default"),
    max_seconds: float | None = typer.Option(None, "--max-seconds", help="Cap on one run's time as a session limit; also its default"),
    max_turns: int | None = typer.Option(None, "--max-turns", help="Cap on a run's model turns; also its default"),
    max_message_chars: int = typer.Option(100_000, "--max-message-chars", help="Longest message a client may send"),
    approval_timeout: float = typer.Option(300.0, "--approval-timeout", help="Seconds before an unanswered approval request is refused"),
    run_timeout: float = typer.Option(3600.0, "--run-timeout", help="Hard limit in seconds on one run"),
    log_level: str = typer.Option("info", "--log-level", help="debug, info, warning or error"),
) -> None:
    """Serve sessions over HTTP: create them, send messages, stream events, answer approvals.

    Clients choose what a session does, within the bounds set here. Every tool runs with this process's own privileges: the permission layers and the audit log apply, but there is no sandbox (see docs/server.md).
    """
    from seawall.config import load_settings
    from seawall.config.paths import get_data_dir

    config = ServerConfig(
        workspace_root=workspace.expanduser().resolve(),
        db_path=(db.expanduser() if db else get_data_dir() / "server" / "sessions.db"),
        token=os.environ.get(token_env) or None,
        host=host,
        port=port,
        max_concurrent_runs=max_concurrent_runs,
        max_queued_runs=max_queued_runs,
        max_loaded_sessions=max_loaded_sessions,
        max_sessions=max_sessions,
        max_message_chars=max_message_chars,
        approval_timeout_seconds=approval_timeout,
        run_timeout_seconds=run_timeout,
        allow_full_auto=allow_full_auto,
        max_budget_usd=max_budget_usd,
        max_total_tokens=max_total_tokens,
        max_seconds=max_seconds,
        max_turns=max_turns,
        allow_no_auth=insecure_no_auth,
    )
    try:
        config.validate()
        from seawall.server.app import check_environment
        from seawall.server.serve import run_server

        check_environment()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(2)
    except ImportError as exc:
        print(
            f"error: the server needs starlette, uvicorn and sse-starlette ({exc}); "
            "install them with: pip install 'seawall[server]'",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    try:
        load_settings().resolve_auth()
    except Exception as exc:
        print(f"warning: no model credentials are configured, so messages will fail ({exc})", file=sys.stderr)
    print(_banner(config), file=sys.stderr, flush=True)
    try:
        run_server(config, log_level=log_level.lower())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(2)


def _banner(config: ServerConfig) -> str:
    auth = "bearer token" if config.token else "NONE (loopback only)"
    lines = [
        f"serving on http://{config.host}:{config.port}",
        f"  workspace   {config.workspace_root}",
        f"  database    {config.db_path}",
        f"  auth        {auth}",
        f"  runs        {config.max_concurrent_runs} at once, {config.max_queued_runs} queued, run limit {config.run_timeout_seconds:g}s",
    ]
    caps = [
        f"{name.replace('_', '-')} {getattr(config, name):g}"
        for name in ("max_budget_usd", "max_total_tokens", "max_seconds", "max_turns")
        if getattr(config, name) is not None
    ]
    if caps:
        lines.append(f"  session caps  {', '.join(caps)}")
    if config.allow_full_auto:
        lines.append("  WARNING     clients may run tools without approval (--allow-full-auto)")
    lines.append("  note        tools run with this process's privileges; there is no sandbox")
    return "\n".join(lines)
