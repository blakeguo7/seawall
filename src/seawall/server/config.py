"""Server configuration."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

TOKEN_ENV = "SEAWALL_SERVER_TOKEN"


class ConfigError(ValueError):
    """The server cannot start with this configuration."""


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ServerConfig:
    """Everything the service needs to know, fixed at start-up.

    Clients decide what a session does (model, tools, limits) but only inside the bounds set
    here: the workspace root, the caps, and whether runs may act without anyone approving.
    """

    workspace_root: Path
    db_path: Path
    token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    max_concurrent_runs: int = 4  # runs executing at once, across all sessions
    max_queued_runs: int = 16  # runs allowed to wait for a slot before new ones get a 429
    max_loaded_sessions: int = 16  # live runtimes kept in memory; idle ones are saved and closed
    max_sessions: int = 1000
    max_message_chars: int = 100_000
    max_pending_events: int = 1000  # events a run may have queued for the database writer
    lease_seconds: float = 60.0  # how long a process owns a session without renewing
    approval_timeout_seconds: float = 300.0  # an unanswered approval request is refused after this
    run_timeout_seconds: float = 3600.0  # hard wall-clock cap on one run
    poll_seconds: float = 1.0  # how often a stream checks the database when nothing wakes it
    heartbeat_seconds: float = 15.0  # keep-alive comment on idle streams
    # Whether a client may start sessions that run mutating tools with nobody approving them:
    # ``permission_mode: full_auto`` or a non-empty ``allowed_tools`` (pre-approving ``bash`` is as
    # strong as ``full_auto`` for shell commands). Off by default.
    allow_full_auto: bool = False
    max_budget_usd: float | None = None  # upper bounds on what a client may ask for
    max_total_tokens: int | None = None
    max_seconds: float | None = None
    max_turns: int | None = None
    allow_no_auth: bool = False
    server_id: str = field(default_factory=lambda: uuid4().hex[:8])  # identifies this process in leases

    @property
    def max_body_bytes(self) -> int:
        """Largest request body read: a full message with every character JSON-escaped, plus slack."""
        return self.max_message_chars * 6 + 64 * 1024

    def validate(self) -> None:
        """Refuse configurations that would be unsafe or meaningless."""
        if not self.workspace_root.is_dir():
            raise ConfigError(f"workspace root {self.workspace_root} is not a directory")
        if self.token is None:
            if not self.allow_no_auth:
                raise ConfigError(
                    f"no token configured; set {TOKEN_ENV}, or pass --insecure-no-auth on a loopback address"
                )
            if not is_loopback(self.host):
                raise ConfigError("running without a token is only allowed on a loopback address")
        elif len(self.token) < 16:
            raise ConfigError("the token must be at least 16 characters")
        if not 0 <= self.port <= 65535:
            raise ConfigError("port must be between 0 and 65535")
        for name in (
            "max_concurrent_runs",
            "max_loaded_sessions",
            "max_sessions",
            "max_message_chars",
            "max_pending_events",
        ):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.max_queued_runs < 0:
            raise ConfigError("max_queued_runs cannot be negative")
        if self.max_loaded_sessions < self.max_concurrent_runs:
            raise ConfigError("max_loaded_sessions must be at least max_concurrent_runs")
        for name in (
            "lease_seconds",
            "approval_timeout_seconds",
            "run_timeout_seconds",
            "poll_seconds",
            "heartbeat_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")
        for name in ("max_budget_usd", "max_total_tokens", "max_seconds", "max_turns"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ConfigError(f"{name} must be positive")
