"""Tests for BackendRegistry: default registration, lookup and custom backends."""

from __future__ import annotations

import pytest

from seawall.swarm.registry import BackendRegistry, get_backend_registry
from seawall.swarm.subprocess_backend import SubprocessBackend
from seawall.swarm.types import TeammateExecutor


def test_registry_registers_subprocess_by_default():
    registry = BackendRegistry()
    assert registry.available_backends() == ["subprocess"]


def test_get_executor_subprocess():
    registry = BackendRegistry()
    executor = registry.get_executor("subprocess")
    assert isinstance(executor, SubprocessBackend)
    assert executor.type == "subprocess"


def test_get_executor_defaults_to_subprocess():
    registry = BackendRegistry()
    executor = registry.get_executor()
    assert executor is registry.get_executor("subprocess")
    assert isinstance(executor, TeammateExecutor)


def test_get_executor_unknown_raises():
    registry = BackendRegistry()
    with pytest.raises(KeyError, match="docker"):
        registry.get_executor("docker")  # type: ignore[arg-type]


def test_register_custom_backend_replaces_default():
    class FakeExecutor:
        type = "subprocess"

        def is_available(self):
            return True

        async def spawn(self, config):
            ...

        async def send_message(self, agent_id, message):
            ...

        async def shutdown(self, agent_id, *, force=False):
            ...

    registry = BackendRegistry()
    fake = FakeExecutor()
    registry.register_backend(fake)
    assert registry.get_executor("subprocess") is fake
    assert registry.get_executor() is fake


def test_health_check_reports_availability():
    report = BackendRegistry().health_check()
    assert report["backends"]["subprocess"] == {"available": True, "type": "subprocess"}
    assert report["total_count"] == 1


def test_reset_restores_defaults():
    registry = BackendRegistry()
    original = registry.get_executor()
    registry.reset()
    assert registry.get_executor() is not original
    assert registry.available_backends() == ["subprocess"]


def test_get_backend_registry_is_a_singleton():
    assert get_backend_registry() is get_backend_registry()
