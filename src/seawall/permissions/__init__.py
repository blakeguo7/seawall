"""Permission helpers for Seawall."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from seawall.permissions.checker import PermissionChecker, PermissionDecision
    from seawall.permissions.modes import PermissionMode

__all__ = ["PermissionChecker", "PermissionDecision", "PermissionMode"]


def __getattr__(name: str):
    if name in {"PermissionChecker", "PermissionDecision"}:
        from seawall.permissions.checker import PermissionChecker, PermissionDecision

        return {
            "PermissionChecker": PermissionChecker,
            "PermissionDecision": PermissionDecision,
        }[name]
    if name == "PermissionMode":
        from seawall.permissions.modes import PermissionMode

        return PermissionMode
    raise AttributeError(name)
