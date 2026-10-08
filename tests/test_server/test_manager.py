"""The session manager on its own: the races and bookkeeping that HTTP tests can only brush against."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from seawall.server.errors import ApiError
from seawall.server.manager import SessionManager
from seawall.server.schemas import SessionSpec
from seawall.server.store import SessionStore
from tests.fakes import ScriptedClient, text_message
from tests.test_server.conftest import GatedClient


@pytest.fixture
async def manager(config, provider):
    store = SessionStore(config.db_path)
    await store.open()
    manager = SessionManager(config, store, client_factory=provider)
    await manager.recover()
    yield manager
    await manager.shutdown()
    await store.close()


async def new_session(manager: SessionManager, **spec) -> str:
    row, _ = await manager.create_session(SessionSpec(**spec))
    return row.id


async def finished(run, timeout: float = 5) -> dict:
    await asyncio.wait_for(run.done.wait(), timeout)
    assert run.result is not None
    return run.result


async def test_a_run_stopped_before_its_task_began_still_ends_cleanly(manager, provider):
    sid = await new_session(manager)
    model = GatedClient()
    provider.script(sid, model)
    run = await manager.submit(sid, "never starts")
    assert manager._stop_run(run, "interrupted", "stopped at once")  # no await since submit: the task has not run yet

    result = await finished(run)
    assert result["stop_reason"] == "interrupted" and result["detail"] == "stopped at once"
    assert not model.started.is_set()
    status = manager.status()
    assert status["running"] == 0 and status["queued"] == 0
    row = await manager.store.get_session(sid)
    assert row.state == "idle" and row.lease_owner is None
    assert manager._live.get(sid) is None  # nothing left behind in memory


async def test_a_run_that_is_finishing_is_not_counted_as_queued(manager, provider, monkeypatch):
    """Its slot is free and its events are out; it is only being closed, so nothing is waiting."""
    gate = asyncio.Event()
    real_release = manager.store.release_lease

    async def held_release(*args, **kwargs):  # the last thing a run does before it is gone
        await gate.wait()
        return await real_release(*args, **kwargs)

    monkeypatch.setattr(manager.store, "release_lease", held_release)
    sid = await new_session(manager)
    run = await manager.submit(sid, "hello")
    for _ in range(500):
        if run.finishing and not run.active:
            break
        await asyncio.sleep(0.01)
    assert run.finishing and not run.active and not run.done.is_set()

    status = manager.status()
    assert status["running"] == 0 and status["queued"] == 0
    gate.set()
    assert (await finished(run))["stop_reason"] == "completed"


async def test_simultaneous_messages_to_one_session_admit_exactly_one(manager, provider):
    sid = await new_session(manager)
    model = GatedClient()
    provider.script(sid, model)
    outcomes = await asyncio.gather(*(manager.submit(sid, f"m{n}") for n in range(12)), return_exceptions=True)
    accepted = [o for o in outcomes if not isinstance(o, Exception)]
    refused = [o for o in outcomes if isinstance(o, ApiError)]
    assert len(accepted) == 1 and len(refused) == 11
    assert {e.code for e in refused} == {"busy"}
    model.release()
    assert (await finished(accepted[0]))["stop_reason"] == "completed"
    assert len(model.requests) == 1  # the model was only ever asked once


async def test_a_refused_submission_leaves_nothing_behind(manager, provider):
    sid = await new_session(manager)
    assert await manager.store.acquire_lease(sid, "another-server", 60)
    with pytest.raises(ApiError) as caught:
        await manager.submit(sid, "hello")
    assert caught.value.code == "session_elsewhere"
    assert manager.status()["queued"] == 0 and manager._live.get(sid) is None
    assert await manager.store.list_runs(sid) == []
    assert (await manager.store.get_session(sid)).state == "idle"

    await manager.store.release_lease(sid, "another-server")
    assert (await finished(await manager.submit(sid, "now it works")))["stop_reason"] == "completed"


async def test_overload_is_counted_exactly(config, provider):
    store = SessionStore(config.db_path)
    await store.open()
    manager = SessionManager(replace(config, max_concurrent_runs=1, max_queued_runs=1, max_loaded_sessions=1), store, client_factory=provider)
    try:
        ids = [await new_session(manager) for _ in range(4)]
        models = [GatedClient() for _ in ids]
        for sid, model in zip(ids, models):
            provider.script(sid, model)
        first = await manager.submit(ids[0], "a")
        second = await manager.submit(ids[1], "b")
        for sid in ids[2:]:
            with pytest.raises(ApiError) as caught:
                await manager.submit(sid, "c")
            assert caught.value.status == 429 and caught.value.retry_after
        for model in models:
            model.release()
        await finished(first)
        await finished(second)
        assert manager._inflight == 0 and manager._running == 0
        third = await manager.submit(ids[2], "now there is room")  # the refusals did not use up capacity
        await finished(third)
    finally:
        await manager.shutdown()
        await store.close()


async def test_work_is_refused_once_shutdown_has_begun(manager):
    sid = await new_session(manager)
    manager.begin_shutdown()
    with pytest.raises(ApiError) as caught:
        await manager.submit(sid, "late")
    assert caught.value.status == 503
    with pytest.raises(ApiError):
        await manager.create_session(SessionSpec())


async def test_shutdown_interrupts_runs_and_closes_runtimes(config, provider):
    store = SessionStore(config.db_path)
    await store.open()
    manager = SessionManager(config, store, client_factory=provider)
    sid = await new_session(manager)
    model = GatedClient()
    provider.script(sid, model)
    run = await manager.submit(sid, "long")
    await asyncio.to_thread(model.started.wait, 5)

    await manager.shutdown()

    result = await finished(run)
    assert result["stop_reason"] == "interrupted" and "shutting down" in result["detail"]
    assert model.cancelled.is_set()
    assert manager.status()["loaded_sessions"] == 0
    row = await store.get_session(sid)
    assert row.state == "idle" and row.lease_owner is None
    await store.close()


# --- reading events ----------------------------------------------------------------------------


async def test_a_stream_pages_through_more_events_than_one_read_returns(manager):
    sid = await new_session(manager)
    for n in range(450):
        await manager.store.append_event(sid, "tool.started", {"n": n})
    await manager.store.append_event(sid, "run.finished", {"run_id": "r"})
    seen = [row.seq async for row in manager.stream_events(sid, 0, until_run="r")]
    assert seen == list(range(1, 452))


async def test_a_stream_picks_up_events_written_by_another_process(manager, config):
    """No notification reaches this process's bus, so only the polling can find them."""
    sid = await new_session(manager)
    other = SessionStore(config.db_path)  # a second connection, as a second server would have
    await other.open()
    try:
        stream = manager.stream_events(sid, 0)
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        await other.append_event(sid, "status", {"message": "from elsewhere"})
        started = time.monotonic()
        row = await asyncio.wait_for(waiting, 3)
        assert row.data == {"message": "from elsewhere"} and time.monotonic() - started < 1
        await stream.aclose()
    finally:
        await other.close()


async def test_a_stream_ends_when_the_server_begins_to_shut_down(manager):
    sid = await new_session(manager)
    stream = manager.stream_events(sid, 0)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.1)
    manager.begin_shutdown()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 2)


async def test_the_stream_of_a_session_that_does_not_exist_ends_at_once(manager):
    """The HTTP layer answers 404 before starting a stream; the generator itself must never hang."""
    assert [row async for row in manager.stream_events("000000000000", 0)] == []
    sid = await new_session(manager)
    stream = manager.stream_events(sid, 0)
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.1)
    await manager.delete_session(sid)
    with pytest.raises(StopAsyncIteration):  # deleted while it was following it
        await asyncio.wait_for(waiting, 2)


async def test_each_session_only_ever_sees_its_own_events(manager, provider):
    a, b = await new_session(manager), await new_session(manager)
    provider.script(a, ScriptedClient(text_message("for a")))
    provider.script(b, ScriptedClient(text_message("for b")))
    run_a, run_b = await asyncio.gather(manager.submit(a, "qa"), manager.submit(b, "qb"))
    await finished(run_a)
    await finished(run_b)
    for sid, text in ((a, "for a"), (b, "for b")):
        rows = [row async for row in manager.stream_events(sid, 0, until_run=run_a.id if sid == a else run_b.id)]
        assert {row.session_id for row in rows} == {sid}
        assert any(row.type == "assistant.message" and row.data["text"] == text for row in rows)


async def test_a_runtime_that_was_being_built_when_the_run_was_interrupted_is_not_abandoned(manager, provider, monkeypatch):
    import seawall.server.manager as module

    built, closed = [], []
    real_build, real_close = module.build_runtime, module.close_runtime

    async def slow_build(**kwargs):
        await asyncio.sleep(0.3)
        bundle = await real_build(**kwargs)
        built.append(bundle)
        return bundle

    async def counting_close(bundle):
        closed.append(bundle)
        await real_close(bundle)

    monkeypatch.setattr(module, "build_runtime", slow_build)
    monkeypatch.setattr(module, "close_runtime", counting_close)

    sid = await new_session(manager)
    run = await manager.submit(sid, "interrupt me while my runtime is built")
    await asyncio.sleep(0.1)  # the build is under way
    assert await manager.interrupt(sid)
    result = await finished(run)

    assert result["stop_reason"] == "interrupted"
    assert len(built) == 1  # the build was allowed to finish ...
    assert manager.status()["loaded_sessions"] == 1  # ... and the runtime is cached, not leaked
    await manager.shutdown()
    assert closed == built  # and closed with everything else


async def test_a_refused_delete_leaves_the_session_and_its_runtime_alone(manager, provider):
    sid = await new_session(manager)
    assert (await finished(await manager.submit(sid, "hi")))["stop_reason"] == "completed"
    assert manager.status()["loaded_sessions"] == 1
    assert await manager.store.acquire_lease(sid, "another-server", 60)

    with pytest.raises(ApiError) as caught:
        await manager.delete_session(sid)
    assert caught.value.code == "session_elsewhere"
    assert manager.status()["loaded_sessions"] == 1  # the cached runtime was not dropped on the floor
    assert manager._live[sid].deleting is False
    assert await manager.store.get_session(sid) is not None

    await manager.store.release_lease(sid, "another-server")
    await manager.delete_session(sid)
    assert await manager.store.get_session(sid) is None and manager.status()["loaded_sessions"] == 0


async def test_a_submission_abandoned_half_way_leaves_no_phantom_run(manager, provider, monkeypatch):
    sid = await new_session(manager)
    real_update = manager.store.update_session

    async def failing_update(session_id, **fields):
        if fields.get("state") == "queued":
            raise asyncio.CancelledError  # the caller is cancelled right after the run row was written
        return await real_update(session_id, **fields)

    # a scoped patch: monkeypatch.undo() would also undo the fixture that isolates HOME
    with monkeypatch.context() as patch:
        patch.setattr(manager.store, "update_session", failing_update)
        with pytest.raises(asyncio.CancelledError):
            await manager.submit(sid, "abandoned")

    runs = await manager.store.list_runs(sid)
    assert len(runs) == 1 and runs[0]["status"] == "error"
    row = await manager.store.get_session(sid)
    assert row.state == "idle" and row.lease_owner is None
    assert manager.status()["queued"] == 0
    assert (await finished(await manager.submit(sid, "and now properly")))["stop_reason"] == "completed"


async def test_the_cache_never_holds_more_runtimes_than_its_size(config, provider, monkeypatch):
    """Builds that start together must not each assume there is room for one more."""
    import seawall.server.manager as module

    store = SessionStore(config.db_path)
    await store.open()
    manager = SessionManager(
        replace(config, max_concurrent_runs=4, max_queued_runs=64, max_loaded_sessions=4), store, client_factory=provider
    )
    open_now, peak = 0, 0
    real_build, real_close = module.build_runtime, module.close_runtime

    async def slow_build(**kwargs):
        nonlocal open_now, peak
        await asyncio.sleep(0.03)  # widens the window in which concurrent builds could overlap
        bundle = await real_build(**kwargs)
        open_now += 1
        peak = max(peak, open_now)
        return bundle

    async def close(bundle):
        nonlocal open_now
        await real_close(bundle)
        open_now -= 1

    monkeypatch.setattr(module, "build_runtime", slow_build)
    monkeypatch.setattr(module, "close_runtime", close)
    try:
        ids = [await new_session(manager) for _ in range(12)]
        for round_number in range(2):  # the second round has idle cached runtimes to evict
            runs = [await manager.submit(sid, f"round {round_number}") for sid in ids]
            for run in runs:
                assert (await finished(run, 20))["stop_reason"] == "completed"
            assert manager.status()["loaded_sessions"] <= 4
        assert peak <= 4, f"{peak} runtimes were open at once with a cache of 4"
    finally:
        await manager.shutdown()
        await store.close()
    assert open_now == 0  # and everything was closed in the end
