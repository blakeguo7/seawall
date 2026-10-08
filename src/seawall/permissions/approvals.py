"""Approval requests: who is asked, how long they have, and what silence means.

When the permission checker says a call needs confirmation, the agent loop builds an
:class:`ApprovalRequest` and hands it to an :class:`Approver`. The answer is an
:class:`ApprovalDecision`, and anything other than ``APPROVED`` means the call does
not run. In particular a missing approver, a timeout and a cancelled session are all
denials, never approvals: a headless run with nobody to ask cannot do what needs asking.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from seawall.permissions.risk import RiskLevel

log = logging.getLogger(__name__)


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"  # there was nobody to ask


@dataclass(frozen=True)
class ApprovalRequest:
    """One tool call waiting for a yes or a no."""

    tool_name: str
    summary: str  # redacted one-liner: the command, path or URL involved
    reason: str  # why the permission policy wants a human to look at it
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: tuple[str, ...] = ()
    session_id: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "reason": self.reason,
            "risk": self.risk.label,
            "risk_reasons": list(self.risk_reasons),
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    """The answer to an :class:`ApprovalRequest`, and who gave it."""

    outcome: ApprovalOutcome
    decided_by: str
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.outcome is ApprovalOutcome.APPROVED


class Approver(Protocol):
    """Anything that can answer an approval request."""

    async def request(self, request: ApprovalRequest) -> ApprovalDecision: ...


class DenyApprover:
    """Approver for runs with nobody to ask: every request is refused."""

    def __init__(self, note: str = "this session is non-interactive, so nothing can approve the call") -> None:
        self._note = note

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(ApprovalOutcome.UNAVAILABLE, "no-approver", self._note)


class AutoApprover:
    """Approves every request. For tests and for callers that opt in explicitly."""

    def __init__(self, decided_by: str = "auto") -> None:
        self._decided_by = decided_by

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(ApprovalOutcome.APPROVED, self._decided_by)


class CallbackApprover:
    """Adapts an ``async (tool_name, reason) -> bool`` prompt to the approver interface.

    Requests are answered one at a time, so a turn that runs several tools at once
    does not stack up overlapping prompts.
    """

    def __init__(
        self,
        callback: Callable[[str, str], Awaitable[bool]],
        *,
        decided_by: str = "user",
        timeout: float | None = None,
    ) -> None:
        self._callback = callback
        self._decided_by = decided_by
        self._timeout = timeout
        self._lock = asyncio.Lock()

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        async with self._lock:
            try:
                approved = await asyncio.wait_for(
                    self._callback(request.tool_name, request.reason), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                return ApprovalDecision(ApprovalOutcome.TIMED_OUT, "timeout")
        outcome = ApprovalOutcome.APPROVED if approved else ApprovalOutcome.DENIED
        return ApprovalDecision(outcome, self._decided_by)


class ApprovalBroker:
    """Holds pending requests until someone outside the agent loop answers them.

    The agent awaits :meth:`request`; a UI, an HTTP handler or a test calls
    :meth:`resolve` with the request id. Unanswered requests are denied after
    ``timeout`` seconds. ``on_request`` is called when a request is registered, so a
    server can tell its clients there is something to answer.

    All methods must be called from the event loop thread.
    """

    def __init__(
        self,
        *,
        timeout: float | None = 300.0,
        on_request: Callable[[ApprovalRequest], Any] | None = None,
    ) -> None:
        self._timeout = timeout
        self._on_request = on_request
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalDecision]]] = {}

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = (request, future)
        try:
            if self._on_request is not None:
                notified = self._on_request(request)
                if inspect.isawaitable(notified):
                    await notified
            try:
                return await asyncio.wait_for(future, timeout=self._timeout)
            except asyncio.TimeoutError:
                note = f"no answer within {self._timeout:g}s" if self._timeout else "no answer"
                return ApprovalDecision(ApprovalOutcome.TIMED_OUT, "timeout", note)
        finally:
            self._pending.pop(request.id, None)

    def pending(self) -> list[ApprovalRequest]:
        """Requests still waiting, oldest first."""
        return sorted((req for req, _ in self._pending.values()), key=lambda req: req.created_at)

    def resolve(
        self, request_id: str, *, approved: bool, decided_by: str = "api", note: str = ""
    ) -> bool:
        """Answer a pending request. Returns False if it is unknown or already answered."""
        entry = self._pending.get(request_id)
        if entry is None or entry[1].done():
            return False
        outcome = ApprovalOutcome.APPROVED if approved else ApprovalOutcome.DENIED
        entry[1].set_result(ApprovalDecision(outcome, decided_by, note))
        return True

    def cancel_all(self, note: str = "session closed") -> int:
        """Deny everything still pending, e.g. when the session ends. Returns the count."""
        count = 0
        for _, future in self._pending.values():
            if not future.done():
                future.set_result(ApprovalDecision(ApprovalOutcome.DENIED, "cancelled", note))
                count += 1
        return count
