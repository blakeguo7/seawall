"""Tracing: a timed tree of what a run did, one JSON line per finished step.

A *span* is one step with a start, a duration, a status and a few attributes: the run as a whole,
one turn of the loop, one model call, one tool call, a wait for approval, a compaction. A span
points at its parent, so a trace file reads back as a tree that shows where the time and the money
went.

The field names (``trace``, ``span``, ``parent``, ``name``, ``start``, ``status``, ``attrs``) follow
OpenTelemetry, so exporting to it is a mapping and not a redesign. Nothing here needs it installed.

Attributes are redacted the way audit records are: a string that holds a credential is masked, and
long text is cut. Message and tool-output text is left out unless ``capture_content`` is on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seawall.audit.redact import redact_text, truncate

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# How a span ended. ``denied``: a tool call that policy, a hook or an approver refused.
# ``stopped``: a run cut short by a limit or the loop guard. ``cancelled``: abandoned midway.
STATUSES = ("ok", "error", "denied", "stopped", "cancelled")


class Span:
    """One timed step. Create it with :meth:`Tracer.start`; finish it once with :meth:`end`."""

    __slots__ = (
        "_tracer",
        "name",
        "trace_id",
        "span_id",
        "parent",
        "attrs",
        "started_at",
        "_started",
        "_ended",
        "_previous",
        "_current",
    )

    def __init__(
        self,
        tracer: Tracer,
        name: str,
        trace_id: str,
        parent: Span | None,
        attrs: dict[str, Any],
        *,
        current: bool,
        previous: Span | None,
    ) -> None:
        self._tracer = tracer
        self.name = name
        self.trace_id = trace_id
        self.span_id = secrets.token_hex(8)
        self.parent = parent
        self.attrs = attrs
        self.started_at = time.time()
        self._started = time.monotonic()
        self._ended = False
        self._current = current
        self._previous = previous

    @property
    def parent_id(self) -> str | None:
        return self.parent.span_id if self.parent is not None else None

    def set(self, **attrs: Any) -> None:
        """Add attributes while the span is open."""
        self.attrs.update(attrs)

    def end(self, status: str = "ok", **attrs: Any) -> None:
        """Finish the span and write it. Ending a span again does nothing."""
        if self._ended:
            return
        self._ended = True
        self.attrs.update(attrs)
        self._tracer._finish(self, status, (time.monotonic() - self._started) * 1000)

    def discard(self) -> None:
        """Finish the span without writing it, for a step that turned out to be a no-op."""
        if self._ended:
            return
        self._ended = True
        self._tracer._release(self)

    def child(self, name: str, **attrs: Any) -> Span:
        return self._tracer.start(name, parent=self, **attrs)

    def __enter__(self) -> Span:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        if exc is None:
            self.end()
        elif isinstance(exc, asyncio.CancelledError):
            self.end("cancelled")
        else:
            self.end("error", error=f"{type(exc).__name__}: {exc}")
        return False


class NullSpan:
    """What a disabled tracer hands out: every call does nothing."""

    trace_id = ""
    span_id = ""
    parent_id = None
    name = ""

    def set(self, **attrs: Any) -> None:
        pass

    def end(self, status: str = "ok", **attrs: Any) -> None:
        pass

    def discard(self) -> None:
        pass

    def child(self, name: str, **attrs: Any) -> NullSpan:
        return self

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


NULL_SPAN = NullSpan()


class NullTracer:
    """Tracing is off: no spans, no files, no cost."""

    enabled = False
    capture_content = False
    current: Span | None = None

    def start(self, name: str, *, parent: object = None, make_current: bool = False, **attrs: Any) -> NullSpan:
        return NULL_SPAN


NULL_TRACER = NullTracer()


class Tracer:
    """Writes the spans of one session to a JSON Lines file, each when it finishes.

    A span that was never finished (a crash) is not in the file. Each write opens the file in
    append mode and closes it again, so nothing is left open and two processes can share a file.
    A file that cannot be written is a warning, once, and the agent carries on untraced.
    """

    enabled = True

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        capture_content: bool = False,
        max_field_chars: int = 1000,
    ) -> None:
        self.path = path
        self.session_id = session_id
        self.capture_content = capture_content
        self._max_field_chars = max_field_chars
        self._current: Span | None = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def current(self) -> Span | None:
        """The span that new spans without an explicit parent attach to."""
        return self._current

    def start(
        self,
        name: str,
        *,
        parent: Span | NullSpan | None = None,
        make_current: bool = False,
        **attrs: Any,
    ) -> Span:
        """Open a span. Without ``parent`` it hangs under the current span, or starts a new trace.

        ``make_current`` makes it the default parent until it ends. Use it for a step that other
        code runs inside (a run, a turn); a step that runs next to its siblings (the tool calls of
        one turn) is given its parent explicitly instead.
        """
        if not isinstance(parent, Span):
            parent = self._current
        trace_id = parent.trace_id if parent is not None else secrets.token_hex(16)
        span = Span(self, name, trace_id, parent, dict(attrs), current=make_current, previous=self._current)
        if make_current:
            self._current = span
        return span

    def _release(self, span: Span) -> None:
        if span._current and self._current is span:
            self._current = span._previous

    def _finish(self, span: Span, status: str, duration_ms: float) -> None:
        self._release(span)
        self._write(
            {
                "v": SCHEMA_VERSION,
                "trace": span.trace_id,
                "span": span.span_id,
                "parent": span.parent_id,
                "session": self.session_id,
                "name": span.name,
                "start": datetime.fromtimestamp(span.started_at, timezone.utc).isoformat(timespec="milliseconds"),
                "ms": round(duration_ms, 1),
                "status": status,
                "attrs": {key: self._clean(value) for key, value in span.attrs.items()},
            }
        )

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return truncate(redact_text(value), self._max_field_chars)
        return value

    def _write(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._failed:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    view = memoryview(line)
                    while view:
                        view = view[os.write(fd, view) :]
                finally:
                    os.close(fd)
            except OSError as exc:
                self._failed = True
                log.warning("trace file %s cannot be written, continuing without tracing: %s", self.path, exc)


AnyTracer = Tracer | NullTracer
