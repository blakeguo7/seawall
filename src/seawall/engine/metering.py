"""Count every model call, wherever in the engine it is made."""

from __future__ import annotations

from typing import Any, AsyncIterator

from seawall.api.client import ApiMessageCompleteEvent, ApiMessageRequest, SupportsStreamingMessages
from seawall.engine.cost_tracker import CostTracker


class MeteredApiClient:
    """Wraps a streaming client and records the usage of each completed call.

    The agent loop, context compaction and memory extraction all receive the same
    wrapped client, so their calls are counted in one place and none is missed.
    """

    def __init__(self, inner: SupportsStreamingMessages, tracker: CostTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[Any]:
        async for event in self._inner.stream_message(request):
            if isinstance(event, ApiMessageCompleteEvent):
                self._tracker.add(event.usage, model=request.model)
            yield event

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
