"""Budget, token, time and loop limits as the agent loop enforces them."""

from __future__ import annotations

from pathlib import Path

import pytest

from seawall.api.usage import UsageSnapshot
from seawall.config.settings import PermissionSettings
from seawall.engine.cost_tracker import CostTracker
from seawall.engine.limits import LoopGuardConfig, RunLimits
from seawall.engine.messages import ToolResultBlock
from seawall.engine.pricing import ModelPrice, PriceTable
from seawall.engine.query import MaxTurnsExceeded
from seawall.engine.query_engine import QueryEngine
from seawall.engine.stream_events import ErrorEvent, RunFinished, StatusEvent
from seawall.permissions import PermissionChecker, PermissionMode
from seawall.tools import create_default_tool_registry
from tests.fakes import GeneratedClient, RecordingAudit, ScriptedClient, text_message, tool_message

# One scripted call uses 1000 input tokens, which this price turns into exactly one dollar.
DOLLAR_PER_CALL = ModelPrice(input=1000, output=0)
ONE_CALL = UsageSnapshot(input_tokens=1000, output_tokens=0)


def make_engine(tmp_path: Path, client, *, limits=None, price=DOLLAR_PER_CALL, audit=None, max_turns=50, model="priced"):
    return QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO)),
        cwd=tmp_path,
        model=model,
        system_prompt="system",
        max_turns=max_turns,
        limits=limits,
        cost_tracker=CostTracker(PriceTable({"priced": price} if price else {})),
        **({"audit": audit} if audit else {}),
    )


def distinct_calls(n: int):
    return tool_message(("bash", {"command": f"echo step{n}"}), turn=n)


def same_call(n: int):
    return tool_message(("bash", {"command": "echo same"}), turn=n)


async def run(engine: QueryEngine, prompt: str = "go"):
    events = []
    try:
        async for event in engine.submit_message(prompt):
            events.append(event)
    except MaxTurnsExceeded as exc:
        events.append(exc)
    return events


def finished(events) -> RunFinished:
    return next(e for e in reversed(events) if isinstance(e, RunFinished))


def statuses(events) -> list[str]:
    return [e.message for e in events if isinstance(e, StatusEvent)]


def every_tool_use_has_a_result(engine: QueryEngine) -> bool:
    uses, results = set(), set()
    for message in engine.messages:
        for block in message.content:
            if getattr(block, "type", "") == "tool_use":
                uses.add(block.id)
            if isinstance(block, ToolResultBlock):
                results.add(block.tool_use_id)
    return uses == results


# --- completion ------------------------------------------------------------------------------------


async def test_a_normal_run_reports_how_it_ended_and_what_it_used(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, ScriptedClient(text_message("hi"), usage=ONE_CALL))
    events = await run(engine)

    done = finished(events)
    assert events[-1] is done
    assert done.stop_reason == "completed"
    assert done.turns == 1
    assert done.usage.input_tokens == 1000
    assert done.cost_usd == pytest.approx(1.0)
    assert done.unpriced_models == ()
    assert engine.last_run == done


async def test_a_run_with_tool_calls_counts_its_turns(tmp_path: Path) -> None:
    client = ScriptedClient(distinct_calls(1), distinct_calls(2), text_message("done"), usage=ONE_CALL)
    done = finished(await run(make_engine(tmp_path, client)))
    assert done.turns == 3 and done.stop_reason == "completed"


# --- budget -----------------------------------------------------------------------------------------


async def test_the_budget_stops_the_run_before_the_next_model_call(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_budget_usd=2.5))
    events = await run(engine)

    assert len(client.requests) == 3  # $3.00 spent: over $2.50 by at most one call, never two
    done = finished(events)
    assert done.stop_reason == "budget_exceeded"
    assert done.turns == 3
    assert done.cost_usd == pytest.approx(3.0)
    assert any("Budget reached: $3.0000 of $2.50" in message for message in statuses(events))
    assert every_tool_use_has_a_result(engine)  # the conversation stays valid


async def test_the_budget_covers_the_whole_session(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_budget_usd=2.5))
    await run(engine, "first")
    calls_after_first = len(client.requests)

    second = await run(engine, "second")

    assert len(client.requests) == calls_after_first  # nothing more was spent
    assert finished(second).stop_reason == "budget_exceeded"
    assert finished(second).turns == 0


async def test_a_dollar_budget_without_a_price_refuses_to_start(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_budget_usd=5), price=None)
    events = await run(engine)

    assert client.requests == []
    assert finished(events).stop_reason == "no_price"
    assert "no price is known for priced" in statuses(events)[0]


async def test_unpriced_models_are_reported_not_charged(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, ScriptedClient(text_message("hi"), usage=ONE_CALL), price=None)
    done = finished(await run(engine))
    assert done.cost_usd == 0 and done.unpriced_models == ("priced",)


# --- tokens and time ----------------------------------------------------------------------------------


async def test_the_token_limit_stops_the_run(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_total_tokens=2500), price=None)
    events = await run(engine)

    assert len(client.requests) == 3
    assert finished(events).stop_reason == "token_limit"
    assert every_tool_use_has_a_result(engine)


class _Clock:
    """A clock that moves one second every time it is read."""

    def __init__(self) -> None:
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += 1.0
        return self._now


async def test_the_time_limit_stops_the_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("seawall.engine.query.time", _Clock())
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_seconds=1.5))
    events = await run(engine)

    done = finished(events)
    assert done.stop_reason == "time_limit"
    assert len(client.requests) == 1  # the first step ran; the check before the second one stopped it
    assert "Time limit reached" in statuses(events)[0]
    assert every_tool_use_has_a_result(engine)


async def test_the_time_limit_restarts_with_every_prompt(tmp_path: Path) -> None:
    client = ScriptedClient(text_message("a"), text_message("b"))
    engine = make_engine(tmp_path, client, limits=RunLimits(max_seconds=60))
    assert finished(await run(engine, "one")).stop_reason == "completed"
    assert finished(await run(engine, "two")).stop_reason == "completed"


# --- loop guard -----------------------------------------------------------------------------------------


async def test_a_repeating_agent_is_warned_and_then_stopped(tmp_path: Path) -> None:
    client = GeneratedClient(same_call, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(loop=LoopGuardConfig()))
    events = await run(engine)

    assert len(client.requests) == 5
    done = finished(events)
    assert done.stop_reason == "loop_detected"
    assert "same call 5 times" in done.detail
    assert every_tool_use_has_a_result(engine)

    def last_tool_result_text(conversation) -> str:
        blocks = conversation[-1].content
        return " ".join(b.content for b in blocks if isinstance(b, ToolResultBlock))

    assert "[loop guard]" not in last_tool_result_text(client.sent[2])  # after the 2nd call
    assert "[loop guard]" in last_tool_result_text(client.sent[3])  # the 3rd call earns a warning
    assert "[loop guard]" not in last_tool_result_text(client.sent[4])  # and only once


async def test_the_loop_guard_can_be_disabled(tmp_path: Path) -> None:
    client = GeneratedClient(same_call, usage=ONE_CALL)
    limits = RunLimits(loop=LoopGuardConfig(enabled=False))
    engine = make_engine(tmp_path, client, limits=limits, max_turns=8)
    events = await run(engine)

    assert isinstance(events[-1], MaxTurnsExceeded)
    assert len(client.requests) == 8


async def test_without_limits_nothing_changes(tmp_path: Path) -> None:
    client = GeneratedClient(same_call, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=None, max_turns=8)
    events = await run(engine)
    assert isinstance(events[-1], MaxTurnsExceeded)


# --- other ways a run ends ------------------------------------------------------------------------------------


async def test_hitting_max_turns_is_reported_before_the_exception(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, max_turns=3)
    events = await run(engine)

    assert isinstance(events[-1], MaxTurnsExceeded)
    done = finished(events)
    assert (done.stop_reason, done.turns) == ("max_turns", 3)
    assert "3" in done.detail


async def test_an_api_error_ends_the_run_as_an_error(tmp_path: Path) -> None:
    class Failing:
        async def stream_message(self, request):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    events = await run(make_engine(tmp_path, Failing()))

    assert any(isinstance(e, ErrorEvent) for e in events)
    done = finished(events)
    assert done.stop_reason == "error" and "boom" in done.detail


async def test_the_end_of_every_run_is_audited(tmp_path: Path) -> None:
    audit = RecordingAudit()
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_budget_usd=1.5), audit=audit)
    await run(engine)

    (record,) = audit.of("run.finished")
    assert record["stop_reason"] == "budget_exceeded"
    assert record["turns"] == 2
    assert record["input_tokens"] == 2000
    assert record["cost_usd"] == pytest.approx(2.0)
    assert record["unpriced_models"] == []


async def test_abandoning_a_run_is_audited_as_interrupted(tmp_path: Path) -> None:
    audit = RecordingAudit()
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, audit=audit)

    stream = engine.submit_message("go")
    async for _event in stream:
        break  # the consumer walks away mid-run
    await stream.aclose()

    assert audit.of("run.finished")[0]["stop_reason"] == "interrupted"


# --- the conversation after a run was cut short ------------------------------------------------------


async def test_the_next_prompt_continues_from_a_valid_conversation(tmp_path: Path) -> None:
    client = GeneratedClient(distinct_calls, usage=ONE_CALL)
    engine = make_engine(tmp_path, client, limits=RunLimits(max_budget_usd=1.5))
    await run(engine, "first")
    assert engine.messages[-1].role == "user"  # the results of the last tool call were kept
    assert every_tool_use_has_a_result(engine)

    engine.set_limits(RunLimits(max_budget_usd=100))  # the user raised the budget
    calls_before = len(client.requests)
    await run(engine, "second")

    conversation = client.sent[calls_before]  # what the model saw on the first call of the second prompt
    answered = {
        block.tool_use_id
        for message in conversation
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    asked = {
        block.id
        for message in conversation
        for block in message.content
        if getattr(block, "type", "") == "tool_use"
    }
    assert asked and asked <= answered


async def test_a_run_stopped_by_max_turns_can_be_continued(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, GeneratedClient(distinct_calls, usage=ONE_CALL), max_turns=2)
    events = await run(engine)
    assert isinstance(events[-1], MaxTurnsExceeded)
    assert engine.has_pending_continuation() is True
