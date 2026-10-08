"""Request and response shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionSpec(BaseModel):
    """What a client may ask for when it creates a session.

    There is deliberately no API key, base URL or sandbox setting here: the server's own
    provider configuration is used, and a client cannot redirect it.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, description="Directory under the workspace root; default is the root itself")
    model: str | None = Field(default=None, max_length=200)
    permission_mode: Literal["default", "plan", "full_auto"] | None = None
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    disallowed_tools: list[str] = Field(default_factory=list, max_length=100)
    append_system_prompt: str | None = Field(default=None, max_length=20_000)
    max_budget_usd: float | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_seconds: float | None = Field(default=None, gt=0)
    max_turns: int | None = Field(default=None, gt=0)

    @field_validator("allowed_tools", "disallowed_tools")
    @classmethod
    def _names_only(cls, names: list[str]) -> list[str]:
        for name in names:
            if not name or len(name) > 100 or any(ch.isspace() or ch == "," for ch in name):
                raise ValueError(f"not a tool name: {name!r}")
        return names


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    note: str = Field(default="", max_length=500)
