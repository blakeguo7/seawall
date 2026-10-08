from __future__ import annotations

import asyncio
import time

import pytest

from seawall.api.usage import UsageSnapshot
from seawall.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from seawall.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    RunFinished,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from seawall.server.events import (
    MAX_TOOL_OUTPUT_CHARS,
    EventBus,
    EventPublisher,
    EventStoreError,
    to_wire,
)
from seawall.server.store import SessionRow, SessionStore


# --- translating engine events -----------------------------------------------------------------


def test_text_and_turn_events():
    assert to_wire(AssistantTextDelta(text="hi")) == ("assistant.delta", {"text": "hi"})
    message = ConversationMessage(
        role="assistant",
        content=[TextBlock(text="looking"), ToolUseBlock(id="t1", name="bash", input={"command": "ls"})],
    )
    kind, data = to_wire(AssistantTurnComplete(message=message, usage=UsageSnapshot(input_tokens=3, output_tokens=4)))
    assert kind == "assistant.message"
    assert data["text"] == "looking"
    assert data["tool_calls"] == [{"id": "t1", "name": "bash"}]
    assert data["usage"] == {"input_tokens": 3, "output_tokens": 4}


def test_tool_inputs_are_redacted_and_file_contents_are_not_sent():
    kind, data = to_wire(
        ToolExecutionStarted(
            tool_name="write_file",
            tool_input={"path": "a.py", "content": "x" * 5000 + " token=sk-ant-" + "a" * 30},
        )
    )
    assert kind == "tool.started"
    shown = str(data["tool_input"])
    assert "a.py" in shown
    assert "sk-ant-" not in shown
    assert "x" * 100 not in shown  # reduced to a length and a digest


def test_tool_results_are_capped_and_say_whether_they_were_refused():
    kind, data = to_wire(ToolExecutionCompleted(tool_name="bash", output="y" * (MAX_TOOL_OUTPUT_CHARS * 2)))
    assert kind == "tool.completed"
    assert len(data["output"]) < MAX_TOOL_OUTPUT_CHARS + 100 and "+" in data["output"][-30:]
    assert data["denied"] is False and data["is_error"] is False

    _, refused = to_wire(
        ToolExecutionCompleted(
            tool_name="bash", output="no", is_error=True, metadata={"denied": True, "denied_by": "policy"}
        )
    )
    assert refused["denied"] is True and refused["denied_by"] == "policy"


def test_errors_status_and_compaction():
    assert to_wire(ErrorEvent(message="boom sk-ant-" + "b" * 30, recoverable=False))[1]["message"].count("sk-ant-") == 0
    assert to_wire(StatusEvent(message="working")) == ("status", {"message": "working"})
    kind, data = to_wire(CompactProgressEvent(phase="compact_start", trigger="auto", attempt=1, message="m"))
    assert kind == "compact" and data["phase"] == "compact_start"


def test_run_finished_is_not_forwarded_by_the_translator():
    assert to_wire(RunFinished(stop_reason="completed", turns=1)) is None


# --- the bus -----------------------------------------------------------------------------------


async def test_notify_wakes_a_waiter_and_only_for_its_session():
    bus = EventBus()
    mine, other = bus.subscribe("s1"), bus.subscribe("s2")
    bus.notify("s1")
    assert await mine.wait(0.5) is True
    assert await other.wait(0.05) is False


async def test_a_notification_between_reading_and_waiting_is_not_lost():
    bus = EventBus()
    waiter = bus.subscribe("s1")
    waiter.clear()
    bus.notify("s1")  # arrives after the (imagined) read, before the wait
    started = time.monotonic()
    assert await waiter.wait(5) is True
    assert time.monotonic() - started < 0.5


async def test_closing_the_bus_wakes_everyone_for_good():
    bus = EventBus()
    waiter = bus.subscribe("s1")
    bus.close()
    assert bus.closed
    waiter.clear()  # must not undo the wake-up
    assert await waiter.wait(0.5) is True
    assert await bus.subscribe("s9").wait(0.5) is True  # late subscribers are woken too


async def test_waiters_deregister():
    bus = EventBus()
    with bus.subscribe("s1"):
        assert bus.subscriber_count() == 1
    assert bus.subscriber_count() == 0


# --- the publisher -----------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path):
    store = SessionStore(tmp_path / "e.db")
    await store.open()
    now = time.time()
    await store.create_session(SessionRow("s1", None, "/w", "idle", {}, now, now))
    yield store
    await store.close()


async def test_events_are_written_in_order_and_listeners_are_woken(store):
    bus = EventBus()
    waiter = bus.subscribe("s1")
    publisher = EventPublisher(store, bus, "s1")
    publisher.start()
    for n in range(5):
        await publisher.emit("tool.started", {"run_id": "r", "n": n})
    await publisher.close()
    assert [e.data["n"] for e in await store.events_after("s1", 0)] == [0, 1, 2, 3, 4]
    assert await waiter.wait(0.5) is True


async def test_deltas_that_pile_up_are_merged_but_other_events_split_them(store):
    class SlowStore:
        """Makes the writer busy so that deltas queue up behind it."""

        def __init__(self, inner):
            self.inner = inner

        async def append_event(self, *args):
            await asyncio.sleep(0.05)
            return await self.inner.append_event(*args)

    publisher = EventPublisher(SlowStore(store), EventBus(), "s1")
    publisher.start()
    await publisher.emit("run.started", {"run_id": "r"})
    for part in ("Hel", "lo ", "wor", "ld"):
        await publisher.emit("assistant.delta", {"run_id": "r", "text": part})
    await publisher.emit("tool.started", {"run_id": "r"})
    await publisher.emit("assistant.delta", {"run_id": "r", "text": "after"})
    await publisher.close()

    stored = await store.events_after("s1", 0)
    assert [e.type for e in stored] == ["run.started", "assistant.delta", "tool.started", "assistant.delta"]
    assert stored[1].data["text"] == "Hello world"  # nothing lost, nothing reordered
    assert stored[3].data["text"] == "after"


async def test_deltas_of_different_runs_are_never_merged(store):
    publisher = EventPublisher(store, EventBus(), "s1")
    publisher.start()
    await publisher.emit("assistant.delta", {"run_id": "r1", "text": "a"})
    await publisher.emit("assistant.delta", {"run_id": "r2", "text": "b"})
    await publisher.close()
    texts = [(e.data["run_id"], e.data["text"]) for e in await store.events_after("s1", 0)]
    assert sorted(texts) == [("r1", "a"), ("r2", "b")]


async def test_a_failing_store_stops_the_run_at_its_next_event(store):
    class BrokenStore:
        async def append_event(self, *args):
            raise OSError("disk full")

    publisher = EventPublisher(BrokenStore(), EventBus(), "s1")
    publisher.start()
    await publisher.emit("run.started", {"run_id": "r"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        try:
            await publisher.emit("assistant.delta", {"run_id": "r", "text": "x"})
        except EventStoreError as exc:
            assert "disk full" in str(exc)
            break
    else:
        pytest.fail("the writer's failure never reached the agent loop")
    await publisher.close()  # still shuts down cleanly


async def test_a_full_queue_makes_the_agent_wait_instead_of_growing(store):
    gate = asyncio.Event()

    class BlockedStore:
        async def append_event(self, *args):
            await gate.wait()
            return (1, 0.0)

    publisher = EventPublisher(BlockedStore(), EventBus(), "s1", max_pending=3)
    publisher.start()
    await publisher.emit("a", {})  # taken by the writer, which then blocks
    await asyncio.sleep(0.02)
    for _ in range(3):
        await publisher.emit("b", {})  # fills the queue
    third = asyncio.ensure_future(publisher.emit("c", {}))
    await asyncio.sleep(0.05)
    assert not third.done()  # backpressure
    gate.set()
    await asyncio.wait_for(third, 2)
    await publisher.close()


async def test_emit_nowait_drops_instead_of_blocking_when_full(store):
    publisher = EventPublisher(store, EventBus(), "s1", max_pending=1)
    publisher.emit_nowait("a", {})
    publisher.emit_nowait("b", {})  # full: dropped, no exception, no wait
    publisher.start()
    await publisher.close()
    assert [e.type for e in await store.events_after("s1", 0)] == ["a"]
