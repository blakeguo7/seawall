"""The events a session sends to its clients, and how they get from the agent loop to them.

The database is the single source of truth. The agent loop hands events to an
:class:`EventPublisher`, which writes them (in order, each under the next sequence number of its
session) and then pokes the :class:`EventBus`. A subscriber, such as an SSE connection, is only a
cursor into the table: it reads what is after the last number it saw and sleeps on the bus until
there is more. So a slow client cannot slow the agent or lose events, a reconnecting client resumes
from its last id with the same code path as a live one, and a second server process sharing the
database still serves the stream (its subscribers fall back to polling).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from seawall.audit import redact_text, summarize_tool_input, truncate
from seawall.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from seawall.server.store import SessionStore

log = logging.getLogger(__name__)

# Tool output is part of the conversation, but an event is a notification: cap it so one huge
# `cat` does not bloat the table. The full text stays in the conversation snapshot.
MAX_TOOL_OUTPUT_CHARS = 20_000

TERMINAL_EVENT = "run.finished"


def to_wire(event: StreamEvent) -> tuple[str, dict[str, Any]] | None:
    """Translate an engine event into ``(type, data)``, or None if clients do not need it.

    ``RunFinished`` is not translated here: the server sends ``run.finished`` itself, last, after
    the session is idle again, so a client that sees it can send the next message at once.
    """
    if isinstance(event, AssistantTextDelta):
        return "assistant.delta", {"text": event.text}
    if isinstance(event, AssistantTurnComplete):
        return "assistant.message", {
            "text": event.message.text,
            "tool_calls": [{"id": use.id, "name": use.name} for use in event.message.tool_uses],
            "usage": {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
            },
        }
    if isinstance(event, ToolExecutionStarted):
        return "tool.started", {
            "tool_name": event.tool_name,
            "tool_input": summarize_tool_input(event.tool_input),
        }
    if isinstance(event, ToolExecutionCompleted):
        metadata = event.metadata or {}
        return "tool.completed", {
            "tool_name": event.tool_name,
            "output": truncate(event.output, MAX_TOOL_OUTPUT_CHARS),
            "is_error": event.is_error,
            "denied": bool(metadata.get("denied")),
            "denied_by": metadata.get("denied_by"),
        }
    if isinstance(event, ErrorEvent):
        return "error", {"message": redact_text(event.message), "recoverable": event.recoverable}
    if isinstance(event, StatusEvent):
        return "status", {"message": event.message}
    if isinstance(event, CompactProgressEvent):
        return "compact", {
            "phase": event.phase,
            "trigger": event.trigger,
            "attempt": event.attempt,
            "message": event.message,
        }
    return None


class EventBus:
    """Wakes the subscribers of a session when it has new events. In-process only."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[Waiter]] = {}
        self._closed = False

    def subscribe(self, session_id: str) -> Waiter:
        waiter = Waiter(self, session_id)
        self._waiters.setdefault(session_id, set()).add(waiter)
        if self._closed:
            waiter._event.set()
        return waiter

    def notify(self, session_id: str) -> None:
        for waiter in tuple(self._waiters.get(session_id, ())):
            waiter._event.set()

    def close(self) -> None:
        """Wake every subscriber for good; used when the server is shutting down."""
        self._closed = True
        for waiters in self._waiters.values():
            for waiter in waiters:
                waiter._event.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def _drop(self, waiter: Waiter) -> None:
        waiters = self._waiters.get(waiter.session_id)
        if waiters is not None:
            waiters.discard(waiter)
            if not waiters:
                del self._waiters[waiter.session_id]

    def subscriber_count(self) -> int:
        return sum(len(waiters) for waiters in self._waiters.values())


class Waiter:
    """One subscriber's wake-up flag.

    Call :meth:`clear` *before* reading the database and :meth:`wait` after: a notification that
    arrives between the read and the wait is kept, so nothing is slept through.
    """

    def __init__(self, bus: EventBus, session_id: str) -> None:
        self._bus = bus
        self.session_id = session_id
        self._event = asyncio.Event()

    def clear(self) -> None:
        if not self._bus.closed:
            self._event.clear()

    async def wait(self, timeout: float) -> bool:
        """True if woken by a notification, False if the timeout passed first."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def close(self) -> None:
        self._bus._drop(self)

    def __enter__(self) -> Waiter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class EventStoreError(RuntimeError):
    """The event log could not be written, so the run cannot continue."""


class EventPublisher:
    """Writes one run's events to the store, in order, without making the agent loop wait.

    :meth:`emit` queues an event and returns. A background task drains the queue; text deltas
    that piled up while it was busy are merged into one event, which keeps the table small when the
    model streams quickly and costs nothing when it does not. If the writer falls
    ``max_pending`` events behind, ``emit`` waits (the agent slows down instead of memory growing).
    If the store fails, the next ``emit`` raises :class:`EventStoreError` and the run ends.
    """

    def __init__(
        self,
        store: SessionStore,
        bus: EventBus,
        session_id: str,
        *,
        max_pending: int = 1000,
    ) -> None:
        self._store = store
        self._bus = bus
        self._session_id = session_id
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(max_pending)
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            self._drain(), name=f"events-{self._session_id}"
        )

    async def emit(self, type_: str, data: Mapping[str, Any]) -> None:
        if self._error is not None:
            raise EventStoreError(f"the event log is not writable: {self._error}") from self._error
        await self._queue.put((type_, dict(data)))

    def emit_nowait(self, type_: str, data: Mapping[str, Any]) -> None:
        """Queue an event without waiting, for code that is being cancelled. Dropped if the queue is full."""
        try:
            self._queue.put_nowait((type_, dict(data)))
        except asyncio.QueueFull:
            log.warning("event %s of session %s dropped: the writer is far behind", type_, self._session_id)

    async def close(self) -> None:
        """Write everything still queued, then stop. Safe to call more than once."""
        task, self._task = self._task, None
        if task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # the caller is being cancelled; let the writer finish on its own
            raise
        if self._error is not None:
            log.error("events of session %s were not all written: %s", self._session_id, self._error)

    async def _drain(self) -> None:
        stopping = False
        while not stopping:
            batch = [await self._queue.get()]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            stopping = batch[-1] is None
            events = _merge_deltas([item for item in batch if item is not None])
            if self._error is None:
                try:
                    for type_, data in events:
                        await self._store.append_event(self._session_id, type_, data)
                except Exception as exc:  # the store is the only thing that can fail here
                    log.exception("cannot write events of session %s", self._session_id)
                    self._error = exc
            self._bus.notify(self._session_id)


def _merge_deltas(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Join runs of consecutive ``assistant.delta`` events from the same run into one."""
    merged: list[tuple[str, dict[str, Any]]] = []
    for type_, data in events:
        if (
            merged
            and type_ == "assistant.delta"
            and merged[-1][0] == "assistant.delta"
            and merged[-1][1].get("run_id") == data.get("run_id")
        ):
            merged[-1] = (type_, {**merged[-1][1], "text": merged[-1][1]["text"] + data["text"]})
        else:
            merged.append((type_, data))
    return merged
