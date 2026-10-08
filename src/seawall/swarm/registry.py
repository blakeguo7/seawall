"""Backend registry for sub-agent execution."""

from __future__ import annotations

import logging
from typing import Any

from seawall.swarm.types import BackendType, TeammateExecutor

logger = logging.getLogger(__name__)

DEFAULT_BACKEND: BackendType = "subprocess"


class BackendRegistry:
    """Registry that maps backend names to :class:`TeammateExecutor` instances.

    The subprocess backend is registered by default and used when no backend
    is named. Other executors can be added with :meth:`register_backend`.

    Usage::

        registry = BackendRegistry()
        executor = registry.get_executor()  # the default (subprocess) backend
    """

    def __init__(self) -> None:
        self._backends: dict[BackendType, TeammateExecutor] = {}
        self._register_defaults()

    def register_backend(self, executor: TeammateExecutor) -> None:
        """Register an executor under its declared ``type`` key."""
        self._backends[executor.type] = executor
        logger.debug("Registered backend: %s", executor.type)

    def get_executor(self, backend: BackendType | None = None) -> TeammateExecutor:
        """Return the executor for ``backend`` (the default backend when omitted).

        Raises:
            KeyError: If the requested backend has not been registered.
        """
        resolved = backend or DEFAULT_BACKEND
        executor = self._backends.get(resolved)
        if executor is None:
            available = list(self._backends.keys())
            raise KeyError(f"Backend {resolved!r} is not registered. Available: {available}")
        return executor

    def available_backends(self) -> list[BackendType]:
        """Return sorted list of registered backend types."""
        return sorted(self._backends.keys())

    def health_check(self) -> dict[str, Any]:
        """Report which registered backends are currently usable."""
        results: dict[str, dict[str, Any]] = {}
        available_count = 0
        for backend_type, executor in self._backends.items():
            is_available = executor.is_available()
            results[backend_type] = {"available": is_available, "type": str(executor.type)}
            if is_available:
                available_count += 1
        return {"backends": results, "total_count": available_count}

    def reset(self) -> None:
        """Drop all registrations and re-register the defaults (for tests)."""
        self._backends.clear()
        self._register_defaults()

    def _register_defaults(self) -> None:
        from seawall.swarm.subprocess_backend import SubprocessBackend

        self._backends["subprocess"] = SubprocessBackend()


_registry: BackendRegistry | None = None


def get_backend_registry() -> BackendRegistry:
    """Return the process-wide singleton BackendRegistry."""
    global _registry
    if _registry is None:
        _registry = BackendRegistry()
    return _registry
