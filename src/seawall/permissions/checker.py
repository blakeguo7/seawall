"""Permission checking for tool execution."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from seawall.config.settings import PermissionSettings
from seawall.permissions.modes import PermissionMode
from seawall.permissions.risk import RiskLevel, assess_tool_call
from seawall.permissions.sensitive import (
    SENSITIVE_PATH_PATTERNS,
    match_sensitive_path,
    policy_match_paths,
)

__all__ = [
    "PathRule",
    "PermissionChecker",
    "PermissionDecision",
    "SENSITIVE_PATH_PATTERNS",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionDecision:
    """Result of checking whether a tool invocation may run."""

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathRule:
    """A glob-based path permission rule."""

    pattern: str
    allow: bool  # True = allow, False = deny


class PermissionChecker:
    """Evaluate tool usage against the configured permission mode and rules."""

    def __init__(self, settings: PermissionSettings) -> None:
        self._settings = settings
        # Parse path rules from settings
        self._path_rules: list[PathRule] = []
        for rule in getattr(settings, "path_rules", []):
            pattern = getattr(rule, "pattern", None) or (rule.get("pattern") if isinstance(rule, dict) else None)
            allow = getattr(rule, "allow", True) if not isinstance(rule, dict) else rule.get("allow", True)
            if isinstance(pattern, str) and pattern.strip():
                self._path_rules.append(PathRule(pattern=pattern.strip(), allow=allow))
            else:
                log.warning(
                    "Skipping path rule with missing, empty, or non-string 'pattern' field: %r",
                    rule,
                )

    @property
    def mode(self) -> PermissionMode:
        return self._settings.mode

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(self._settings.allowed_tools)

    @property
    def denied_tools(self) -> tuple[str, ...]:
        return tuple(self._settings.denied_tools)

    def evaluate(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        file_path: str | None = None,
        command: str | None = None,
        cwd: str | Path | None = None,
    ) -> PermissionDecision:
        """Return whether the tool may run immediately.

        Two gates come first and cannot be unlocked by the permission mode, the
        allow list or an approval: credential paths, and calls whose static risk
        label is ``critical`` (see :mod:`seawall.permissions.risk`). The
        remaining rules decide between allow, ask and deny.
        """
        # Built-in sensitive path protection — always active, cannot be
        # overridden by user settings or permission mode.  This is a
        # defence-in-depth measure against LLM-directed or prompt-injection
        # driven access to credential files.
        if file_path:
            pattern = match_sensitive_path(file_path)
            if pattern is not None:
                return PermissionDecision(
                    allowed=False,
                    reason=(
                        f"Access denied: {file_path} is a sensitive credential path "
                        f"(matched built-in pattern '{pattern}')"
                    ),
                    risk=RiskLevel.CRITICAL,
                    risk_reasons=("touches a credential path",),
                )

        assessment = assess_tool_call(
            tool_name,
            is_read_only=is_read_only,
            file_path=file_path,
            command=command,
            cwd=cwd,
        )
        if assessment.level >= RiskLevel.CRITICAL:
            return PermissionDecision(
                allowed=False,
                reason=(
                    "Blocked as critical risk; no permission mode or approval can unlock it: "
                    + "; ".join(assessment.reasons)
                ),
                risk=assessment.level,
                risk_reasons=assessment.reasons,
            )

        decision = self._evaluate_policy(
            tool_name, is_read_only=is_read_only, file_path=file_path, command=command
        )
        return replace(decision, risk=assessment.level, risk_reasons=assessment.reasons)

    def _evaluate_policy(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        file_path: str | None,
        command: str | None,
    ) -> PermissionDecision:
        # Explicit tool deny list (tool names are matched without regard to case, so
        # ``--disallowed-tools Bash`` and ``bash`` mean the same thing)
        if _listed(tool_name, self._settings.denied_tools):
            return PermissionDecision(allowed=False, reason=f"{tool_name} is explicitly denied")

        # Explicit tool allow list
        if _listed(tool_name, self._settings.allowed_tools):
            return PermissionDecision(allowed=True, reason=f"{tool_name} is explicitly allowed")

        # Check path-level rules
        if file_path and self._path_rules:
            for candidate_path in policy_match_paths(file_path):
                for rule in self._path_rules:
                    if fnmatch.fnmatch(candidate_path, rule.pattern):
                        if not rule.allow:
                            return PermissionDecision(
                                allowed=False,
                                reason=f"Path {file_path} matches deny rule: {rule.pattern}",
                            )

        # Check command deny patterns (e.g. deny "rm -rf /")
        if command:
            for pattern in getattr(self._settings, "denied_commands", []):
                if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                    return PermissionDecision(
                        allowed=False,
                        reason=f"Command matches deny pattern: {pattern}",
                    )

        # Full auto: allow everything
        if self._settings.mode == PermissionMode.FULL_AUTO:
            return PermissionDecision(allowed=True, reason="Auto mode allows all tools")

        # Read-only tools always allowed
        if is_read_only:
            return PermissionDecision(allowed=True, reason="read-only tools are allowed")

        # Plan mode: block mutating tools
        if self._settings.mode == PermissionMode.PLAN:
            return PermissionDecision(
                allowed=False,
                reason="Plan mode blocks mutating tools until the user exits plan mode",
            )

        # Default mode: require confirmation for mutating tools
        bash_hint = _bash_permission_hint(command)
        reason = (
            "Mutating tools require user confirmation in default mode. "
            "Approve the prompt when asked, or run /permissions full_auto "
            "if you want to allow them for this session."
        )
        if bash_hint:
            reason = f"{reason} {bash_hint}"
        return PermissionDecision(
            allowed=False,
            requires_confirmation=True,
            reason=reason,
        )


def _listed(tool_name: str, names: list[str]) -> bool:
    lowered = tool_name.lower()
    return any(lowered == name.lower() for name in names)


def _bash_permission_hint(command: str | None) -> str:
    if not command:
        return ""
    lowered = command.lower()
    install_markers = (
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "pip install",
        "uv pip install",
        "poetry install",
        "cargo install",
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "npx create-",
        "npm init ",
        "pnpm init ",
        "yarn init ",
    )
    if any(marker in lowered for marker in install_markers):
        return (
            "Package installation and scaffolding commands change the workspace, "
            "so they will not run automatically in default mode."
        )
    return ""
