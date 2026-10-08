"""Tests for approvers and the approval broker."""

from __future__ import annotations

import asyncio

import pytest

from seawall.permissions.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    AutoApprover,
    CallbackApprover,
    DenyApprover,
)
from seawall.permissions.risk import RiskLevel


def make_request(**overrides) -> ApprovalRequest:
    values = {"tool_name": "bash", "summary": "rm -rf build", "reason": "needs confirmation"}
    values.update(overrides)
    return ApprovalRequest(**values)


async def test_deny_approver_refuses_and_says_nobody_could_answer() -> None:
    decision = await DenyApprover().request(make_request())
    assert decision.outcome is ApprovalOutcome.UNAVAILABLE
    assert decision.approved is False
    assert "non-interactive" in decision.note


async def test_auto_approver_approves() -> None:
    decision = await AutoApprover().request(make_request())
    assert decision.approved is True
    assert decision.decided_by == "auto"


async def test_callback_approver_maps_the_boolean() -> None:
    async def yes(tool_name: str, reason: str) -> bool:
        assert (tool_name, reason) == ("bash", "needs confirmation")
        return True

    async def no(tool_name: str, reason: str) -> bool:
        return False

    approved = await CallbackApprover(yes).request(make_request())
    denied = await CallbackApprover(no).request(make_request())
    assert (approved.outcome, approved.decided_by) == (ApprovalOutcome.APPROVED, "user")
    assert denied.outcome is ApprovalOutcome.DENIED


async def test_callback_approver_times_out() -> None:
    async def never(tool_name: str, reason: str) -> bool:
        await asyncio.sleep(30)
        return True

    decision = await CallbackApprover(never, timeout=0.05).request(make_request())
    assert decision.outcome is ApprovalOutcome.TIMED_OUT
    assert decision.approved is False


async def test_callback_approver_asks_one_question_at_a_time() -> None:
    active = 0
    peak = 0

    async def slow_yes(tool_name: str, reason: str) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    approver = CallbackApprover(slow_yes)
    await asyncio.gather(*(approver.request(make_request()) for _ in range(5)))
    assert peak == 1


# --- broker ---------------------------------------------------------------------


async def test_broker_waits_for_an_outside_answer() -> None:
    broker = ApprovalBroker(timeout=5)
    request = make_request()
    waiting = asyncio.create_task(broker.request(request))
    await asyncio.sleep(0)

    assert [pending.id for pending in broker.pending()] == [request.id]
    assert broker.resolve(request.id, approved=True, decided_by="api:alice", note="ok") is True
    decision = await waiting

    assert decision == ApprovalDecision(ApprovalOutcome.APPROVED, "api:alice", "ok")
    assert broker.pending() == []


async def test_broker_denies_on_timeout_and_forgets_the_request() -> None:
    broker = ApprovalBroker(timeout=0.05)
    request = make_request()
    decision = await broker.request(request)

    assert decision.outcome is ApprovalOutcome.TIMED_OUT
    assert decision.decided_by == "timeout"
    assert broker.pending() == []
    # A late answer to a request that already timed out is refused, not applied.
    assert broker.resolve(request.id, approved=True) is False


async def test_broker_resolve_only_works_once() -> None:
    broker = ApprovalBroker(timeout=5)
    request = make_request()
    waiting = asyncio.create_task(broker.request(request))
    await asyncio.sleep(0)

    assert broker.resolve(request.id, approved=False) is True
    assert broker.resolve(request.id, approved=True) is False
    assert (await waiting).outcome is ApprovalOutcome.DENIED


async def test_broker_unknown_id() -> None:
    assert ApprovalBroker().resolve("nope", approved=True) is False


async def test_broker_cancel_all_denies_everything_pending() -> None:
    broker = ApprovalBroker(timeout=5)
    first, second = make_request(), make_request()
    tasks = [asyncio.create_task(broker.request(r)) for r in (first, second)]
    await asyncio.sleep(0)

    assert broker.cancel_all("session closed") == 2
    decisions = await asyncio.gather(*tasks)
    assert {d.outcome for d in decisions} == {ApprovalOutcome.DENIED}
    assert {d.decided_by for d in decisions} == {"cancelled"}


async def test_broker_lists_pending_oldest_first_and_notifies() -> None:
    seen: list[str] = []
    broker = ApprovalBroker(timeout=5, on_request=lambda req: seen.append(req.id))
    older = make_request(created_at=1.0)
    newer = make_request(created_at=2.0)
    tasks = [asyncio.create_task(broker.request(r)) for r in (newer, older)]
    await asyncio.sleep(0)

    assert [r.id for r in broker.pending()] == [older.id, newer.id]
    assert sorted(seen) == sorted([older.id, newer.id])
    broker.cancel_all()
    await asyncio.gather(*tasks)


async def test_broker_supports_an_async_notifier() -> None:
    notified = asyncio.Event()

    async def notify(request: ApprovalRequest) -> None:
        notified.set()

    broker = ApprovalBroker(timeout=5, on_request=notify)
    task = asyncio.create_task(broker.request(make_request()))
    await asyncio.wait_for(notified.wait(), timeout=1)
    broker.cancel_all()
    await task


async def test_cancelling_the_agent_removes_the_pending_request() -> None:
    broker = ApprovalBroker(timeout=5)
    task = asyncio.create_task(broker.request(make_request()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending() == []


def test_request_serialises_for_clients() -> None:
    request = make_request(risk=RiskLevel.HIGH, risk_reasons=("deletes recursively",), session_id="s1")
    data = request.to_dict()
    assert data["risk"] == "high"
    assert data["risk_reasons"] == ["deletes recursively"]
    assert data["session_id"] == "s1"
    assert data["id"] == request.id
