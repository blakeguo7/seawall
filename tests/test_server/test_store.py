from __future__ import annotations

import asyncio
import sqlite3
import stat
import time

import pytest

from seawall.server.store import SessionRow, SessionStore


@pytest.fixture
async def store(tmp_path):
    store = SessionStore(tmp_path / "data" / "server.db")
    await store.open()
    yield store
    await store.close()


def row(session_id: str = "aaaaaaaaaaaa", **fields) -> SessionRow:
    now = time.time()
    defaults = dict(id=session_id, title="t", cwd="/w", state="idle", spec={"model": None}, created_at=now, updated_at=now)
    return SessionRow(**{**defaults, **fields})


async def test_sessions_round_trip(store):
    await store.create_session(row("aaaaaaaaaaaa", title="one", spec={"max_turns": 3}))
    found = await store.get_session("aaaaaaaaaaaa")
    assert found is not None and found.title == "one" and found.spec == {"max_turns": 3}
    assert found.input_tokens == 0 and found.unpriced_models is None
    assert await store.get_session("missing") is None
    assert await store.count_sessions() == 1

    await store.update_session(
        "aaaaaaaaaaaa", state="running", input_tokens=7, cost_usd=0.5, unpriced_models=["x-model"], title="renamed"
    )
    found = await store.get_session("aaaaaaaaaaaa")
    assert found.state == "running" and found.input_tokens == 7 and found.title == "renamed"
    assert found.unpriced_models == ["x-model"]
    assert found.to_dict()["cost_usd"] is None  # an unpriced model makes the cost unknown, not zero


async def test_update_refuses_unknown_and_protected_fields(store):
    await store.create_session(row())
    with pytest.raises(ValueError):
        await store.update_session("aaaaaaaaaaaa", lease_owner="me")
    with pytest.raises(ValueError):
        await store.update_session("aaaaaaaaaaaa", cwd="/etc")


async def test_sessions_list_newest_first_with_paging(store):
    for index, name in enumerate(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]):
        await store.create_session(row(name, updated_at=1000.0 + index))
    page = await store.list_sessions(limit=2)
    assert [r.id for r in page] == ["cccccccccccc", "bbbbbbbbbbbb"]
    assert [r.id for r in await store.list_sessions(limit=2, offset=2)] == ["aaaaaaaaaaaa"]


async def test_event_numbers_are_per_session_and_gapless(store):
    await store.create_session(row("aaaaaaaaaaaa"))
    await store.create_session(row("bbbbbbbbbbbb"))
    assert [(await store.append_event("aaaaaaaaaaaa", "x", {"n": n}))[0] for n in range(3)] == [1, 2, 3]
    assert (await store.append_event("bbbbbbbbbbbb", "x", {}))[0] == 1
    assert await store.last_seq("aaaaaaaaaaaa") == 3
    assert await store.last_seq("nobody") == 0

    after_one = await store.events_after("aaaaaaaaaaaa", 1)
    assert [e.seq for e in after_one] == [2, 3] and after_one[0].data == {"n": 1}
    assert [e.seq for e in await store.events_after("aaaaaaaaaaaa", 0, limit=2)] == [1, 2]
    assert await store.events_after("aaaaaaaaaaaa", 3) == []


async def test_two_connections_never_hand_out_the_same_event_number(tmp_path):
    """Two server processes share one file; numbering must not collide."""
    path = tmp_path / "shared.db"
    first, second = SessionStore(path), SessionStore(path)
    await first.open()
    await second.open()
    try:
        await first.create_session(row())

        async def burst(store: SessionStore, count: int) -> list[int]:
            return [(await store.append_event("aaaaaaaaaaaa", "x", {}))[0] for _ in range(count)]

        left, right = await asyncio.gather(burst(first, 40), burst(second, 40))
        assert sorted(left + right) == list(range(1, 81))
    finally:
        await first.close()
        await second.close()


async def test_snapshots_overwrite(store):
    await store.create_session(row())
    assert await store.load_snapshot("aaaaaaaaaaaa") is None
    await store.save_snapshot("aaaaaaaaaaaa", [{"role": "user"}], {"a": 1})
    await store.save_snapshot("aaaaaaaaaaaa", [{"role": "user"}, {"role": "assistant"}], {"a": 2})
    messages, metadata = await store.load_snapshot("aaaaaaaaaaaa")
    assert len(messages) == 2 and metadata == {"a": 2}


async def test_approvals_lifecycle(store):
    await store.create_session(row())
    approval = {
        "id": "ap1", "session_id": "aaaaaaaaaaaa", "run_id": "r1", "tool_name": "bash", "summary": "ls",
        "reason": "needs approval", "risk": "medium", "risk_reasons": ["why"], "created_at": 1.0,
    }
    await store.save_approval(approval)
    pending = await store.list_approvals("aaaaaaaaaaaa", status="pending")
    assert [a["id"] for a in pending] == ["ap1"] and pending[0]["risk_reasons"] == ["why"]

    await store.resolve_approval("ap1", "approved", "api", "fine")
    assert await store.list_approvals("aaaaaaaaaaaa", status="pending") == []
    got = await store.get_approval("aaaaaaaaaaaa", "ap1")
    assert got["status"] == "approved" and got["decided_by"] == "api" and got["note"] == "fine"
    assert await store.get_approval("bbbbbbbbbbbb", "ap1") is None  # scoped to its session


async def test_runs_lifecycle(store):
    await store.create_session(row())
    await store.create_run("r1", "aaaaaaaaaaaa", "hello")
    assert (await store.list_runs("aaaaaaaaaaaa"))[0]["status"] == "queued"
    await store.mark_run_started("r1")
    assert (await store.list_runs("aaaaaaaaaaaa"))[0]["status"] == "running"
    await store.finish_run("r1", stop_reason="completed", detail="", turns=2)
    done = (await store.list_runs("aaaaaaaaaaaa"))[0]
    assert done["status"] == "finished" and done["turns"] == 2
    await store.create_run("r2", "aaaaaaaaaaaa", "again")
    await store.finish_run("r2", stop_reason="budget_exceeded", detail="d", turns=1)
    assert (await store.list_runs("aaaaaaaaaaaa"))[0]["status"] == "budget_exceeded"


async def test_leases(store):
    await store.create_session(row())
    sid = "aaaaaaaaaaaa"
    assert await store.acquire_lease(sid, "server-a", 30)
    assert await store.acquire_lease(sid, "server-a", 30)  # renewing is fine
    assert not await store.acquire_lease(sid, "server-b", 30)
    await store.release_lease(sid, "server-b")  # not the owner: no effect
    assert not await store.acquire_lease(sid, "server-b", 30)
    await store.release_lease(sid, "server-a")
    assert await store.acquire_lease(sid, "server-b", 30)
    assert not await store.acquire_lease("missing", "server-a", 30)


async def test_an_expired_lease_can_be_taken_over(store):
    await store.create_session(row())
    assert await store.acquire_lease("aaaaaaaaaaaa", "dead-server", 0.05)
    await asyncio.sleep(0.1)
    assert await store.acquire_lease("aaaaaaaaaaaa", "new-server", 30)


async def test_reset_interrupted_ends_orphans_and_spares_live_leases(store):
    for sid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        await store.create_session(row(sid, state="running"))
        await store.create_run(f"run-{sid[0]}", sid, "p")
        await store.mark_run_started(f"run-{sid[0]}")
        await store.save_approval(
            {"id": f"ap-{sid[0]}", "session_id": sid, "run_id": None, "tool_name": "bash", "summary": "s",
             "reason": "r", "risk": "low", "risk_reasons": [], "created_at": 1.0}
        )
    assert await store.acquire_lease("bbbbbbbbbbbb", "another-server", 60)  # alive elsewhere

    ended = await store.reset_interrupted()

    assert ended == [{"run_id": "run-a", "session_id": "aaaaaaaaaaaa"}]
    assert (await store.get_session("aaaaaaaaaaaa")).state == "idle"
    assert (await store.list_runs("aaaaaaaaaaaa"))[0]["status"] == "interrupted"
    assert (await store.get_approval("aaaaaaaaaaaa", "ap-a"))["status"] == "expired"
    # the session another server is running is left exactly as it was
    assert (await store.get_session("bbbbbbbbbbbb")).state == "running"
    assert (await store.list_runs("bbbbbbbbbbbb"))[0]["status"] == "running"
    assert (await store.get_approval("bbbbbbbbbbbb", "ap-b"))["status"] == "pending"


async def test_delete_removes_everything_that_belongs_to_the_session(store):
    await store.create_session(row("aaaaaaaaaaaa"))
    await store.create_session(row("bbbbbbbbbbbb"))
    for sid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        await store.append_event(sid, "x", {})
        await store.save_snapshot(sid, [], {})
        await store.create_run(f"r-{sid[0]}", sid, "p")
    await store.delete_session("aaaaaaaaaaaa")
    assert await store.get_session("aaaaaaaaaaaa") is None
    assert await store.events_after("aaaaaaaaaaaa", 0) == []
    assert await store.load_snapshot("aaaaaaaaaaaa") is None
    assert await store.list_runs("aaaaaaaaaaaa") == []
    assert len(await store.events_after("bbbbbbbbbbbb", 0)) == 1  # the other session is untouched


async def test_the_database_file_is_private_and_uses_wal(store, tmp_path):
    path = tmp_path / "data" / "server.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_a_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer version"):
        await SessionStore(path).open()


async def test_using_a_closed_store_is_an_error(tmp_path):
    store = SessionStore(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="not open"):
        await store.count_sessions()
