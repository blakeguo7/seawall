"""Approval requests answered over the API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Protocol

from seawall.permissions.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalRequest,
)
from seawall.server.store import SessionStore

log = logging.getLogger(__name__)


class RunEvents(Protocol):
    """Where a session's approvals announce themselves: the event stream of its current run."""

    @property
    def run_id(self) -> str | None: ...

    async def emit(self, type_: str, data: Mapping[str, Any]) -> None: ...

    def emit_nowait(self, type_: str, data: Mapping[str, Any]) -> None: ...


class SessionApprover:
    """Asks the API's clients, and treats silence as a refusal.

    A request is stored, announced as an ``approval.requested`` event, and then waits for
    :meth:`resolve` (the ``POST .../approvals/{id}`` endpoint) or for the timeout, which counts as
    a refusal like everywhere else. If the run is interrupted while waiting, the request is
    closed as ``cancelled``.
    """

    def __init__(
        self,
        session_id: str,
        store: SessionStore,
        events: RunEvents,
        *,
        timeout: float,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._events = events
        self._broker = ApprovalBroker(timeout=timeout, on_request=self._announce)

    async def _announce(self, request: ApprovalRequest) -> None:
        record = {**request.to_dict(), "session_id": self._session_id, "run_id": self._events.run_id}
        await self._store.save_approval(record)
        await self._events.emit("approval.requested", record)

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        try:
            decision = await self._broker.request(request)
        except asyncio.CancelledError:
            self._events.emit_nowait(
                "approval.resolved", {"id": request.id, "status": "cancelled", "decided_by": "server"}
            )
            await asyncio.shield(self._close(request.id, "cancelled", "server", "the run was interrupted"))
            raise
        status = decision.outcome.value
        await self._close(request.id, status, decision.decided_by, decision.note)
        await self._events.emit(
            "approval.resolved",
            {"id": request.id, "status": status, "decided_by": decision.decided_by, "note": decision.note},
        )
        return decision

    async def _close(self, approval_id: str, status: str, decided_by: str, note: str) -> None:
        try:
            await self._store.resolve_approval(approval_id, status, decided_by, note)
        except Exception:
            log.exception("cannot record the answer to approval %s", approval_id)

    def resolve(self, approval_id: str, *, approved: bool, note: str = "", decided_by: str = "api") -> bool:
        """Answer a waiting request. False if it is not waiting here (unknown, answered or expired)."""
        return self._broker.resolve(approval_id, approved=approved, decided_by=decided_by, note=note)

    def pending_ids(self) -> list[str]:
        return [request.id for request in self._broker.pending()]

    def cancel_all(self, note: str) -> int:
        """Refuse everything still waiting (the run is over). Returns how many there were."""
        return self._broker.cancel_all(note)
