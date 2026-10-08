"""Sub-agent spawning: executor protocol, backend registry and the subprocess backend."""

from __future__ import annotations

from seawall.swarm.registry import BackendRegistry, get_backend_registry
from seawall.swarm.subprocess_backend import SubprocessBackend
from seawall.swarm.types import (
    BackendType,
    SpawnResult,
    TeammateExecutor,
    TeammateMessage,
    TeammateSpawnConfig,
)

__all__ = [
    "BackendRegistry",
    "BackendType",
    "SpawnResult",
    "SubprocessBackend",
    "TeammateExecutor",
    "TeammateMessage",
    "TeammateSpawnConfig",
    "get_backend_registry",
]
