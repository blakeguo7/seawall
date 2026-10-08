"""Tool abstractions."""

from __future__ import annotations

import copy
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from seawall.hooks.executor import HookExecutor


_SCHEMA_CACHE: weakref.WeakKeyDictionary[type[BaseModel], dict[str, Any]] = weakref.WeakKeyDictionary()


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of a tool's input model, computed once per model class.

    Generating it takes a few hundred microseconds per tool and the agent loop asks for every
    tool's schema on every model call, so a process serving several sessions would otherwise spend
    a noticeable share of its event loop on it. Callers get a copy and may change it.
    """
    cached = _SCHEMA_CACHE.get(model)
    if cached is None:
        cached = _SCHEMA_CACHE[model] = model.model_json_schema()
    return copy.deepcopy(cached)


@dataclass
class ToolExecutionContext:
    """Shared execution context for tool invocations."""

    cwd: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    hook_executor: HookExecutor | None = None


@dataclass(frozen=True)
class ToolResult:
    """Normalized tool execution result."""

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """Base class for all Seawall tools."""

    name: str
    description: str
    input_model: type[BaseModel]

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool."""

    def is_read_only(self, arguments: BaseModel) -> bool:
        """Return whether the invocation is read-only."""
        del arguments
        return False

    def to_api_schema(self) -> dict[str, Any]:
        """Return the tool schema expected by the Anthropic Messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": input_schema(self.input_model),
        }


class ToolRegistry:
    """Map tool names to implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Return a registered tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def to_api_schema(self) -> list[dict[str, Any]]:
        """Return all tool schemas in API format."""
        return [tool.to_api_schema() for tool in self._tools.values()]
