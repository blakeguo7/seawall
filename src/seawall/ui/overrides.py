"""Settings given on the command line that apply to one session only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from seawall.config.settings import normalize_tool_names


@dataclass(frozen=True)
class SessionOverrides:
    """The command-line options that adjust a session without touching settings.json.

    One object travels from the CLI through the launcher and backend host to
    ``build_runtime``, so a new option is added here and in ``cli.py``, not in six
    function signatures.
    """

    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    append_system_prompt: str | None = None
    max_budget_usd: float | None = None
    max_total_tokens: int | None = None
    max_seconds: float | None = None

    @classmethod
    def from_cli(
        cls,
        *,
        allowed_tools: Iterable[str] | str | None = None,
        disallowed_tools: Iterable[str] | str | None = None,
        append_system_prompt: str | None = None,
        max_budget_usd: float | None = None,
        max_total_tokens: int | None = None,
        max_seconds: float | None = None,
    ) -> SessionOverrides:
        return cls(
            allowed_tools=tuple(normalize_tool_names(allowed_tools)),
            disallowed_tools=tuple(normalize_tool_names(disallowed_tools)),
            append_system_prompt=append_system_prompt or None,
            max_budget_usd=max_budget_usd,
            max_total_tokens=max_total_tokens,
            max_seconds=max_seconds,
        )

    def settings_overrides(self) -> dict[str, Any]:
        """Keyword arguments for :meth:`Settings.merge_cli_overrides` (unset ones are None)."""
        return {
            "allowed_tools": list(self.allowed_tools) or None,
            "disallowed_tools": list(self.disallowed_tools) or None,
            "append_system_prompt": self.append_system_prompt,
            "max_budget_usd": self.max_budget_usd,
            "max_total_tokens": self.max_total_tokens,
            "max_seconds": self.max_seconds,
        }

    def to_cli_args(self) -> list[str]:
        """The flags that recreate these overrides in a child ``seawall`` process."""
        args: list[str] = []
        if self.allowed_tools:
            args += ["--allowed-tools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            args += ["--disallowed-tools", ",".join(self.disallowed_tools)]
        if self.append_system_prompt:
            args += ["--append-system-prompt", self.append_system_prompt]
        if self.max_budget_usd is not None:
            args += ["--max-budget-usd", repr(self.max_budget_usd)]
        if self.max_total_tokens is not None:
            args += ["--max-total-tokens", str(self.max_total_tokens)]
        if self.max_seconds is not None:
            args += ["--max-seconds", repr(self.max_seconds)]
        return args
