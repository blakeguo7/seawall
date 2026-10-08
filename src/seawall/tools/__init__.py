"""Built-in tool registration."""

from seawall.tools.ask_user_question_tool import AskUserQuestionTool
from seawall.tools.agent_tool import AgentTool
from seawall.tools.bash_tool import BashTool
from seawall.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult
from seawall.tools.brief_tool import BriefTool
from seawall.tools.config_tool import ConfigTool
from seawall.tools.cron_create_tool import CronCreateTool
from seawall.tools.cron_delete_tool import CronDeleteTool
from seawall.tools.cron_list_tool import CronListTool
from seawall.tools.cron_toggle_tool import CronToggleTool
from seawall.tools.enter_plan_mode_tool import EnterPlanModeTool
from seawall.tools.enter_worktree_tool import EnterWorktreeTool
from seawall.tools.exit_plan_mode_tool import ExitPlanModeTool
from seawall.tools.exit_worktree_tool import ExitWorktreeTool
from seawall.tools.file_edit_tool import FileEditTool
from seawall.tools.file_read_tool import FileReadTool
from seawall.tools.file_write_tool import FileWriteTool
from seawall.tools.glob_tool import GlobTool
from seawall.tools.grep_tool import GrepTool
from seawall.tools.image_generation_tool import ImageGenerationTool
from seawall.tools.image_to_text_tool import ImageToTextTool
from seawall.tools.list_mcp_resources_tool import ListMcpResourcesTool
from seawall.tools.lsp_tool import LspTool
from seawall.tools.mcp_auth_tool import McpAuthTool
from seawall.tools.mcp_tool import McpToolAdapter
from seawall.tools.notebook_edit_tool import NotebookEditTool
from seawall.tools.read_mcp_resource_tool import ReadMcpResourceTool
from seawall.tools.remote_trigger_tool import RemoteTriggerTool
from seawall.tools.send_message_tool import SendMessageTool
from seawall.tools.skill_tool import SkillTool
from seawall.tools.sleep_tool import SleepTool
from seawall.tools.task_create_tool import TaskCreateTool
from seawall.tools.task_get_tool import TaskGetTool
from seawall.tools.task_list_tool import TaskListTool
from seawall.tools.task_output_tool import TaskOutputTool
from seawall.tools.task_stop_tool import TaskStopTool
from seawall.tools.task_update_tool import TaskUpdateTool
from seawall.tools.team_create_tool import TeamCreateTool
from seawall.tools.team_delete_tool import TeamDeleteTool
from seawall.tools.todo_write_tool import TodoWriteTool
from seawall.tools.tool_search_tool import ToolSearchTool
from seawall.tools.web_fetch_tool import WebFetchTool
from seawall.tools.web_search_tool import WebSearchTool


def create_default_tool_registry(mcp_manager=None) -> ToolRegistry:
    """Return the default built-in tool registry."""
    registry = ToolRegistry()
    for tool in (
        BashTool(),
        AskUserQuestionTool(),
        FileReadTool(),
        FileWriteTool(),
        FileEditTool(),
        NotebookEditTool(),
        LspTool(),
        McpAuthTool(),
        GlobTool(),
        GrepTool(),
        ImageToTextTool(),
        ImageGenerationTool(),
        SkillTool(),
        ToolSearchTool(),
        WebFetchTool(),
        WebSearchTool(),
        ConfigTool(),
        BriefTool(),
        SleepTool(),
        EnterWorktreeTool(),
        ExitWorktreeTool(),
        TodoWriteTool(),
        EnterPlanModeTool(),
        ExitPlanModeTool(),
        CronCreateTool(),
        CronListTool(),
        CronDeleteTool(),
        CronToggleTool(),
        RemoteTriggerTool(),
        TaskCreateTool(),
        TaskGetTool(),
        TaskListTool(),
        TaskStopTool(),
        TaskOutputTool(),
        TaskUpdateTool(),
        AgentTool(),
        SendMessageTool(),
        TeamCreateTool(),
        TeamDeleteTool(),
    ):
        registry.register(tool)
    if mcp_manager is not None:
        registry.register(ListMcpResourcesTool(mcp_manager))
        registry.register(ReadMcpResourceTool(mcp_manager))
        for tool_info in mcp_manager.list_tools():
            registry.register(McpToolAdapter(mcp_manager, tool_info))
    return registry


__all__ = [
    "BaseTool",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "create_default_tool_registry",
]
