"""Tool for listing local cron jobs."""

from __future__ import annotations

from pydantic import BaseModel

from seawall.services.cron import load_cron_jobs
from seawall.services.cron_scheduler import is_scheduler_running
from seawall.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronListToolInput(BaseModel):
    """Arguments for cron listing."""


class CronListTool(BaseTool):
    """List local cron jobs."""

    name = "cron_list"
    description = "List configured local cron jobs with schedule, status, and next run time."
    input_model = CronListToolInput

    def is_read_only(self, arguments: CronListToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: CronListToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del arguments, context
        jobs = load_cron_jobs()
        if not jobs:
            return ToolResult(output="No cron jobs configured.")

        scheduler = "running" if is_scheduler_running() else "stopped"
        lines = [f"Scheduler: {scheduler}", ""]

        for job in jobs:
            enabled = "on" if job.get("enabled", True) else "off"
            last_run = job.get("last_run", "never")
            if last_run != "never":
                last_run = last_run[:19]
            next_run = job.get("next_run", "n/a")
            if next_run != "n/a":
                next_run = next_run[:19]
            last_status = job.get("last_status", "")
            status_str = f" ({last_status})" if last_status else ""
            timezone = f" ({job['timezone']})" if job.get("timezone") else ""
            payload = job.get("payload")
            payload_line = ""
            if isinstance(payload, dict):
                payload_line = f"\n     payload: {payload.get('kind', 'agent_turn')}"
            command = job.get("command") or "(agent_turn)"
            lines.append(
                f"[{enabled}] {job['name']}  {job.get('schedule', '?')}{timezone}\n"
                f"     cmd: {command}"
                f"{payload_line}\n"
                f"     last: {last_run}{status_str}  next: {next_run}"
            )
        return ToolResult(output="\n".join(lines))
