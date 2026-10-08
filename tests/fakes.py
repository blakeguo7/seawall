"""Small fakes shared by tests that drive the agent loop."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from seawall.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from seawall.api.usage import UsageSnapshot
from seawall.audit import AuditWriteError
from seawall.engine.messages import ConversationMessage, TextBlock, ToolUseBlock


def text_message(text: str) -> ConversationMessage:
    return ConversationMessage(role="assistant", content=[TextBlock(text=text)])


def tool_message(*calls: tuple[str, dict[str, Any]], turn: int = 0) -> ConversationMessage:
    """An assistant message asking for one or more tool calls: ``("bash", {"command": "ls"})``.

    Tool-call ids must be unique across a conversation; pass ``turn`` when a script makes several.
    """
    prefix = f"toolu_{turn}_" if turn else "toolu_"
    return ConversationMessage(
        role="assistant",
        content=[
            ToolUseBlock(id=f"{prefix}{index}", name=name, input=tool_input)
            for index, (name, tool_input) in enumerate(calls, 1)
        ],
    )


class ScriptedClient:
    """Streaming model client that replays a fixed list of assistant messages."""

    def __init__(self, *messages: ConversationMessage, usage: UsageSnapshot | None = None) -> None:
        self._messages = list(messages)
        self._usage = usage or UsageSnapshot(input_tokens=10, output_tokens=5)
        self.requests: list[Any] = []

    async def stream_message(self, request):
        self.requests.append(request)
        message = self._messages.pop(0) if self._messages else text_message("(script exhausted)")
        for block in message.content:
            if isinstance(block, TextBlock) and block.text:
                yield ApiTextDeltaEvent(text=block.text)
        yield ApiMessageCompleteEvent(message=message, usage=self._usage, stop_reason=None)


class RecordingAudit:
    """Audit sink that keeps records in memory so tests can read them."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._fail_on = fail_on

    def record(self, event_type: str, **data: Any) -> None:
        if event_type == self._fail_on:
            raise AuditWriteError("disk full")
        self.records.append((event_type, data))

    def close(self) -> None:
        self.closed = True

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.records]

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [data for kind, data in self.records if kind == event_type]


class GeneratedClient:
    """Streaming client that builds each assistant message from the call number (1, 2, 3, ...)."""

    def __init__(
        self,
        make_message: Callable[[int], ConversationMessage],
        *,
        usage: UsageSnapshot | None = None,
        delay: float = 0.0,
    ) -> None:
        self._make_message = make_message
        self._usage = usage or UsageSnapshot(input_tokens=1000, output_tokens=500)
        self._delay = delay
        self.requests: list[Any] = []
        self.sent: list[list[ConversationMessage]] = []  # the conversation as it was at each call

    async def stream_message(self, request):
        self.requests.append(request)
        self.sent.append([message.model_copy(deep=True) for message in request.messages])
        if self._delay:
            await asyncio.sleep(self._delay)
        message = self._make_message(len(self.requests))
        yield ApiMessageCompleteEvent(message=message, usage=self._usage, stop_reason=None)
