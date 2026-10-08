"""Backend type definitions for sub-agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

BackendType = Literal["subprocess"]
"""Execution backends. Each registered executor declares one of these names."""


@dataclass
class TeammateSpawnConfig:
    """Configuration for spawning a sub-agent."""

    name: str
    """Human-readable agent name (e.g. ``"researcher"``)."""

    team: str
    """Team name this agent belongs to."""

    prompt: str
    """Initial prompt / task for the agent."""

    cwd: str
    """Working directory for the agent."""

    parent_session_id: str
    """Parent session ID (for transcript correlation)."""

    model: str | None = None
    """Model override for this agent."""

    command: str | None = None
    """Optional explicit command override for the spawned process."""

    system_prompt: str | None = None
    """System prompt resolved from the agent definition."""

    system_prompt_mode: Literal["default", "replace", "append"] | None = None
    """How to apply the system prompt: replace or append to default."""

    permissions: list[str] = field(default_factory=list)
    """Tool permissions to grant this agent."""

    plan_mode_required: bool = False
    """Whether this agent must enter plan mode before implementing."""

    permission_mode: str | None = None
    """The parent's permission mode. The agent runs under it and, having nobody to ask,
    has any call that needs confirmation refused."""

    allowed_tools: list[str] = field(default_factory=list)
    """Tools the parent session allows without asking; inherited by the agent."""

    disallowed_tools: list[str] = field(default_factory=list)
    """Tools the parent session denies; inherited by the agent."""

    task_type: Literal["local_agent", "remote_agent"] = "local_agent"
    """Background task type recorded for the spawned process."""


@dataclass
class SpawnResult:
    """Result from spawning a sub-agent."""

    task_id: str
    """Task ID in the task manager."""

    agent_id: str
    """Unique agent identifier (format: agentName@teamName)."""

    backend_type: BackendType
    """The backend used to spawn this agent."""

    success: bool = True
    error: str | None = None


@dataclass
class TeammateMessage:
    """Message to send to a running agent."""

    text: str
    from_agent: str
    color: str | None = None
    timestamp: str | None = None
    summary: str | None = None


@runtime_checkable
class TeammateExecutor(Protocol):
    """Protocol for sub-agent execution backends.

    Abstracts spawn / messaging / shutdown so that a new backend (for example a
    sandboxed one) can be registered without touching the agent tool.
    """

    type: BackendType

    def is_available(self) -> bool:
        """Check if this backend is available on the system."""
        ...

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """Spawn a new agent with the given configuration."""
        ...

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """Send a message to a running agent via stdin."""
        ...

    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool:
        """Terminate an agent.

        Args:
            agent_id: The agent to terminate.
            force: If True, kill immediately. If False, attempt graceful shutdown.

        Returns:
            True if the agent was terminated successfully.
        """
        ...
