"""Count and time every model call, wherever in the engine it is made."""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from seawall.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiTextDeltaEvent,
    SupportsStreamingMessages,
)
from seawall.audit.redact import truncate
from seawall.engine.cost_tracker import CostTracker
from seawall.tracing import NULL_TRACER, AnyTracer


class MeteredApiClient:
    """Wraps a streaming client and records the usage of each completed call.

    The agent loop, context compaction and memory extraction all receive the same
    wrapped client, so their calls are counted in one place and none is missed. Each call is
    also a ``model.call`` span: where it hangs in the trace tells which of them it was.
    """

    def __init__(
        self,
        inner: SupportsStreamingMessages,
        tracker: CostTracker,
        tracer: AnyTracer = NULL_TRACER,
    ) -> None:
        self._inner = inner
        self._tracker = tracker
        self._tracer = tracer

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[Any]:
        span = self._tracer.start("model.call", model=request.model)
        started = time.monotonic()
        first_text_ms: float | None = None
        retries = 0
        status = "error"  # until the stream reaches its final message
        try:
            async for event in self._inner.stream_message(request):
                if isinstance(event, ApiTextDeltaEvent):
                    if first_text_ms is None:
                        first_text_ms = round((time.monotonic() - started) * 1000, 1)
                elif isinstance(event, ApiRetryEvent):
                    retries += 1
                elif isinstance(event, ApiMessageCompleteEvent):
                    self._tracker.add(event.usage, model=request.model)
                    status = "ok"
                    span.set(
                        input_tokens=event.usage.input_tokens,
                        output_tokens=event.usage.output_tokens,
                        cache_read_tokens=event.usage.cache_read_input_tokens,
                        cache_write_tokens=event.usage.cache_creation_input_tokens,
                        cost_usd=self._tracker.prices.cost(request.model, event.usage),
                        api_stop_reason=event.stop_reason,
                        tool_calls=len(event.message.tool_uses),
                    )
                    if self._tracer.capture_content:
                        span.set(output_preview=truncate(event.message.text, 300))
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        except Exception as exc:
            span.set(error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.end(status, retries=retries, first_text_ms=first_text_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
