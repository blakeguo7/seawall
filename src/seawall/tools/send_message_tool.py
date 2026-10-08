"""Tool for writing messages to running agent tasks."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from seawall.swarm.registry import get_backend_registry
from seawall.swarm.types import TeammateMessage
from seawall.tasks.manager import get_task_manager
from seawall.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class SendMessageToolInput(BaseModel):
    """Arguments for sending a follow-up message to a task."""

    task_id: str = Field(description="Target local agent task id or agent_id (name@team)")
    message: str = Field(description="Message to write to the task stdin")


class SendMessageTool(BaseTool):
    """Send a message to a running local agent task."""

    name = "send_message"
    description = "Send a follow-up message to a running local agent task."
    input_model = SendMessageToolInput

    async def execute(self, arguments: SendMessageToolInput, context: ToolExecutionContext) -> ToolResult:
        del context
        # Spawned agents are addressed as name@team; plain task IDs address tasks directly
        if "@" in arguments.task_id:
            return await self._send_agent_message(arguments.task_id, arguments.message)
        try:
            await get_task_manager().write_to_task(arguments.task_id, arguments.message)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to task {arguments.task_id}")

    async def _send_agent_message(self, agent_id: str, message: str) -> ToolResult:
        """Route a message to a spawned agent via the backend."""
        registry = get_backend_registry()
        # The SubprocessBackend tracks agent_id -> task_id mappings so
        # send_message resolves correctly for any agent spawned by AgentTool.
        executor = registry.get_executor("subprocess")

        teammate_msg = TeammateMessage(text=message, from_agent="coordinator")
        try:
            await executor.send_message(agent_id, teammate_msg)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except Exception as exc:
            logger.error("Failed to send message to %s: %s", agent_id, exc)
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to agent {agent_id}")
