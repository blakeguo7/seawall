"""What the sandbox container is started with, and what environment goes into it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from seawall.config.settings import DockerSandboxSettings, Settings
from seawall.sandbox.docker_backend import DockerSandboxSession, _added_to_host_env

IMAGE = "seawall-sandbox:latest"


@pytest.fixture(autouse=True)
def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("seawall.sandbox.docker_backend.shutil.which", lambda name: "/usr/bin/docker")


def argv_for(**docker) -> list[str]:
    settings = Settings(sandbox={"enabled": True, "backend": "docker", "docker": DockerSandboxSettings(**docker)})
    return DockerSandboxSession(settings=settings, session_id="abc", cwd=Path("/repo"))._build_run_argv()


def value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_the_container_is_closed_down_by_default() -> None:
    argv = argv_for()

    assert value_after(argv, "--network") == "none"
    assert value_after(argv, "--cap-drop") == "ALL"
    assert value_after(argv, "--security-opt") == "no-new-privileges"
    assert "--read-only" in argv
    assert value_after(argv, "--tmpfs") == "/tmp:rw,nosuid,nodev,size=512m"
    assert value_after(argv, "--pids-limit") == "512"
    assert value_after(argv, "--cpus") == "2.0"
    assert "--init" in argv


def test_memory_is_capped_and_swap_is_off() -> None:
    argv = argv_for(memory_limit="1g")

    assert value_after(argv, "--memory") == "1g"
    assert value_after(argv, "--memory-swap") == "1g"  # equal to memory: no swap


def test_there_is_no_swap_flag_without_a_memory_limit() -> None:
    argv = argv_for(memory_limit="")

    assert "--memory" not in argv and "--memory-swap" not in argv


def test_every_restriction_can_be_switched_off() -> None:
    argv = argv_for(
        cpu_limit=0, memory_limit="", pids_limit=0, cap_drop_all=False, no_new_privileges=False, read_only_root=False
    )

    for flag in ("--cpus", "--memory", "--pids-limit", "--cap-drop", "--security-opt", "--read-only", "--tmpfs"):
        assert flag not in argv
    assert "HOME=/tmp" not in argv  # nothing to point it away from
    assert value_after(argv, "--network") == "none"  # the network has no switch


def test_the_size_of_tmp_can_be_chosen() -> None:
    assert value_after(argv_for(tmp_size="64m"), "--tmpfs") == "/tmp:rw,nosuid,nodev,size=64m"


def test_tmp_is_not_noexec_because_build_and_test_tools_run_from_it() -> None:
    assert "noexec" not in value_after(argv_for(), "--tmpfs")


def test_home_points_at_the_writable_tmp_unless_the_user_says_otherwise() -> None:
    default = argv_for()
    chosen = argv_for(extra_env={"HOME": "/work"})

    assert "HOME=/tmp" in default
    assert "HOME=/work" in chosen and "HOME=/tmp" not in chosen


def test_the_container_carries_labels_that_find_it_again() -> None:
    argv = argv_for()

    labels = [argv[i + 1] for i, token in enumerate(argv) if token == "--label"]
    assert labels == ["seawall.sandbox=1", "seawall.session=abc"]


def test_all_options_come_before_the_image_and_the_project_is_still_mounted() -> None:
    argv = argv_for()
    image = argv.index(IMAGE)

    assert argv[image:] == [IMAGE, "tail", "-f", "/dev/null"]
    assert all(argv.index(flag) < image for flag in ("--cap-drop", "--read-only", "--pids-limit", "-v", "-w"))
    assert "/repo:/repo" in argv and value_after(argv, "-w") == "/repo"


def test_the_defaults_are_the_documented_ones() -> None:
    defaults = DockerSandboxSettings()

    assert (defaults.cpu_limit, defaults.memory_limit, defaults.pids_limit) == (2.0, "4g", 512)
    assert defaults.cap_drop_all and defaults.no_new_privileges and defaults.read_only_root
    assert defaults.tmp_size == "512m"


# --- the environment that goes into the container ----------------------------------------------


def test_only_what_the_caller_added_to_the_host_environment_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-host-secret")
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("CHANGED", "host-value")
    import os

    env = {**os.environ, "SEAWALL_HOOK_EVENT": "pre_tool_use", "CHANGED": "caller-value"}

    passed = _added_to_host_env(env)

    assert passed == {"SEAWALL_HOOK_EVENT": "pre_tool_use", "CHANGED": "caller-value"}


def test_no_environment_means_nothing_is_passed() -> None:
    assert _added_to_host_env(None) == {} and _added_to_host_env({}) == {}


async def test_host_secrets_do_not_follow_a_command_into_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-host-secret")
    settings = Settings(sandbox={"enabled": True, "backend": "docker"})
    session = DockerSandboxSession(settings=settings, session_id="abc", cwd=Path("/repo"))
    session._running = True
    captured: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await session.exec_command(
        ["sh", "-c", "env"], cwd="/repo", env={**os.environ, "SEAWALL_HOOK_EVENT": "stop"}
    )

    assert "SEAWALL_HOOK_EVENT=stop" in captured
    joined = " ".join(captured)
    assert "sk-host-secret" not in joined and "ANTHROPIC_API_KEY" not in joined
    assert not any(token.startswith(("PATH=", "HOME=")) for token in captured)
