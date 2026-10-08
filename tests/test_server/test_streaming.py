"""Event streams over a real socket: timing, resuming, isolation and shutdown."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.fakes import ScriptedClient, text_message
from tests.test_server.conftest import (
    GatedClient,
    StreamingClient,
    create_session,
    parse_sse,
    run_message,
    wait_for,
)


def iter_events(response: httpx.Response):
    """Yield SSE events from a streaming response as they arrive."""
    fields: dict[str, str] = {}
    for line in response.iter_lines():
        if line == "":
            if "data" in fields:
                yield {"id": fields.get("id"), "event": fields.get("event"), "data": json.loads(fields["data"])}
            fields = {}
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            fields[name] = value[1:] if value.startswith(" ") else value


def read_events(response: httpx.Response, *, stop_after: int | None = None, until: str | None = None) -> list[dict]:
    """Collect events until the stream ends, ``stop_after`` events have come, or ``until`` has been seen."""
    events: list[dict] = []
    for event in iter_events(response):
        events.append(event)
        if (stop_after is not None and len(events) >= stop_after) or event["event"] == until:
            break
    return events


def seqs(events: list[dict]) -> list[int]:
    return [e["data"]["seq"] for e in events]


def test_events_are_delivered_while_the_run_is_still_going(live, provider):
    with live.http() as http:
        session = create_session(http)
        provider.script(session["id"], StreamingClient(["one ", "two ", "three"], pause=0.3))
        with http.stream("GET", f"/v1/sessions/{session['id']}/events") as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            started = time.monotonic()
            http.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"})
            arrivals: dict[str, float] = {}
            for event in iter_events(stream):
                arrivals.setdefault(event["event"], time.monotonic() - started)
                if event["event"] == "run.finished":
                    break
    # three pauses of 0.3 s separate the start from the end; the start must not have waited for them
    assert arrivals["run.finished"] - arrivals["run.started"] > 0.6
    assert arrivals["run.finished"] - arrivals["assistant.delta"] > 0.3


def test_a_client_that_drops_can_resume_from_the_last_id_without_loss_or_repeats(live, provider):
    with live.http() as http:
        session = create_session(http)
        provider.script(session["id"], StreamingClient(["a ", "b ", "c ", "d "], pause=0.15))
        accepted = http.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"}).json()

        with http.stream("GET", accepted["events_url"]) as first:
            seen = read_events(first, stop_after=2)  # then the connection is dropped
        assert len(seen) == 2

        with http.stream("GET", accepted["events_url"], headers={"Last-Event-ID": seen[-1]["id"]}) as second:
            rest = read_events(second)

        combined = seen + rest
        assert combined[-1]["event"] == "run.finished"
        assert seqs(combined) == list(range(seqs(combined)[0], seqs(combined)[-1] + 1))  # no gaps, no repeats

        with http.stream("GET", f"/v1/sessions/{session['id']}/events?until_run={accepted['run_id']}") as whole:
            full = read_events(whole)
        text = lambda events: "".join(  # noqa: E731
            e["data"]["data"]["text"] for e in events if e["event"] == "assistant.delta"
        )
        assert text(combined) == text(full) == "a b c d "  # whichever way the deltas were grouped


def test_after_and_last_event_id_mean_the_same_thing(live, provider):
    with live.http() as http:
        session = create_session(http)
        result = run_message(http, session["id"])
        base = f"/v1/sessions/{session['id']}/events?until_run={result['run_id']}"
        everything = parse_sse(http.get(base).text)
        cut = everything[1]["data"]["seq"]
        by_param = parse_sse(http.get(f"{base}&after={cut}").text)
        by_header = parse_sse(http.get(base, headers={"Last-Event-ID": str(cut)}).text)
        assert seqs(by_param) == seqs(by_header) == [e["data"]["seq"] for e in everything if e["data"]["seq"] > cut]


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_a_bad_resume_point_is_a_400(live, bad):
    with live.http() as http:
        session = create_session(http)
        assert http.get(f"/v1/sessions/{session['id']}/events?after={bad}").status_code == 400
        assert http.get(f"/v1/sessions/{session['id']}/events", headers={"Last-Event-ID": bad}).status_code == 400


def test_every_subscriber_gets_every_event(live, provider):
    with live.http() as http:
        session = create_session(http)
        provider.script(session["id"], StreamingClient(["x", "y"], pause=0.1))
        results: list[list[dict]] = []

        def subscribe() -> None:
            with live.http() as mine, mine.stream("GET", f"/v1/sessions/{session['id']}/events") as stream:
                results.append(read_events(stream, until="run.finished"))

        threads = [threading.Thread(target=subscribe) for _ in range(5)]
        for t in threads:
            t.start()
        wait_for(lambda: http.get("/v1/status").json()["subscribers"] == 5, what="all subscribers connected")
        run_message(http, session["id"])
        for t in threads:
            t.join(15)
        assert len(results) == 5
        assert all(seqs(r) == seqs(results[0]) for r in results)
        assert results[0][-1]["event"] == "run.finished"
        wait_for(lambda: http.get("/v1/status").json()["subscribers"] == 0, what="subscribers to be released")


def test_a_subscriber_that_never_reads_cannot_slow_the_run_or_lose_events(live, provider):
    with live.http() as http:
        session = create_session(http)
        provider.script(session["id"], StreamingClient([f"piece-{n} " * 20 for n in range(40)], pause=0.0))
        stalled = http.build_request("GET", f"/v1/sessions/{session['id']}/events")
        response = http.send(stalled, stream=True)  # connected, then never read
        try:
            started = time.monotonic()
            result = run_message(http, session["id"])
            assert time.monotonic() - started < 5
            assert result["stop_reason"] == "completed"
            with http.stream("GET", f"/v1/sessions/{session['id']}/events?until_run={result['run_id']}") as fresh:
                events = read_events(fresh)
            assert events[-1]["event"] == "run.finished"
            assert "piece-39" in "".join(e["data"]["data"].get("text", "") for e in events if e["event"] == "assistant.delta")
        finally:
            response.close()


def test_an_idle_stream_stays_open_and_sends_keepalives(make_live):
    server = make_live(heartbeat_seconds=0.2)
    with server.http() as http:
        session = create_session(http)
        with http.stream("GET", f"/v1/sessions/{session['id']}/events") as stream:
            lines: list[str] = []
            deadline = time.monotonic() + 1.0
            for line in stream.iter_lines():
                lines.append(line)
                if time.monotonic() > deadline:
                    break
        assert any(line.startswith(": ping") or line.startswith(":ping") for line in lines)


def test_deleting_a_session_closes_its_streams(live):
    with live.http() as http:
        session = create_session(http)
        ended = threading.Event()

        def follow() -> None:
            with live.http() as mine, mine.stream("GET", f"/v1/sessions/{session['id']}/events") as stream:
                for _ in stream.iter_lines():
                    pass
            ended.set()

        thread = threading.Thread(target=follow, daemon=True)
        thread.start()
        wait_for(lambda: http.get("/v1/status").json()["subscribers"] == 1)
        assert http.delete(f"/v1/sessions/{session['id']}").status_code == 204
        assert ended.wait(5), "the stream stayed open after its session was deleted"


def test_a_run_goes_on_when_the_client_that_started_it_disconnects(live, provider):
    with live.http() as http:
        session = create_session(http)
        model = GatedClient("survived")
        provider.script(session["id"], model)
        with http.stream(
            "POST", f"/v1/sessions/{session['id']}/messages", json={"text": "go"}, headers={"Accept": "text/event-stream"}
        ) as stream:
            first = read_events(stream, stop_after=1)
            assert first[0]["event"] == "run.started"
        # the connection is gone; the model has not answered yet
        assert model.started.wait(5)
        assert http.get(f"/v1/sessions/{session['id']}").json()["state"] == "running"
        model.release()
        wait_for(lambda: http.get(f"/v1/sessions/{session['id']}").json()["state"] == "idle", what="the run to finish")
        runs = http.get(f"/v1/sessions/{session['id']}/runs").json()["runs"]
        assert runs[0]["status"] == "finished"
        conversation = http.get(f"/v1/sessions/{session['id']}/messages").json()["messages"]
        assert conversation[-1]["role"] == "assistant"


def test_shutdown_ends_open_streams_and_saves_the_interrupted_run(make_live, provider, config):
    server = make_live()
    with server.http() as http:
        session = create_session(http)
        model = GatedClient()
        provider.script(session["id"], model)
        http.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"})
        assert model.started.wait(5)
        closed = threading.Event()

        def follow() -> None:
            try:
                with server.http() as mine, mine.stream("GET", f"/v1/sessions/{session['id']}/events") as stream:
                    for _ in stream.iter_lines():
                        pass
            except httpx.HTTPError:
                pass
            closed.set()

        threading.Thread(target=follow, daemon=True).start()
        wait_for(lambda: http.get("/v1/status").json()["subscribers"] == 1)

    started = time.monotonic()
    server.stop()
    assert server.stopped, "the server did not stop"
    assert time.monotonic() - started < 8  # well inside the graceful timer: the streams were woken, not waited out
    assert closed.wait(5)

    import asyncio

    from seawall.server.store import SessionStore

    async def last_event():
        store = SessionStore(config.db_path)
        await store.open()
        try:
            events = await store.events_after(session["id"], 0)
            row = await store.get_session(session["id"])
            return events[-1], row
        finally:
            await store.close()

    event, row = asyncio.run(last_event())
    assert event.type == "run.finished" and event.data["stop_reason"] == "interrupted"
    assert "shutting down" in event.data["detail"]
    assert row.state == "idle" and row.lease_owner is None
    assert model.cancelled.is_set()


def test_authentication_applies_to_streams_too(live):
    with live.http() as http:
        session = create_session(http)
    bare = httpx.Client(base_url=live.url, timeout=5, trust_env=False)
    assert bare.get(f"/v1/sessions/{session['id']}/events").status_code == 401
    assert bare.get("/healthz").status_code == 200
    wrong = httpx.Client(
        base_url=live.url, timeout=5, trust_env=False, headers={"Authorization": "Bearer nope-nope-nope-nope-nope"}
    )
    assert wrong.get(f"/v1/sessions/{session['id']}/events").status_code == 401


def test_many_clients_at_once(make_live, provider):
    server = make_live(max_concurrent_runs=4, max_queued_runs=64, max_loaded_sessions=8)
    with server.http() as admin:
        sessions = [create_session(admin, title=f"s{n}")["id"] for n in range(24)]
        for n, sid in enumerate(sessions):
            provider.script(sid, ScriptedClient(text_message(f"answer {n}")))

        def work(item: tuple[int, str]) -> tuple[int, dict]:
            n, sid = item
            with server.http() as mine:
                return n, run_message(mine, sid, f"q{n}")

        with ThreadPoolExecutor(max_workers=24) as pool:
            results = dict(pool.map(work, enumerate(sessions)))

        assert [results[n]["text"] for n in range(24)] == [f"answer {n}" for n in range(24)]
        assert all(r["stop_reason"] == "completed" for r in results.values())
        status = admin.get("/v1/status").json()
        assert status["running"] == 0 and status["queued"] == 0 and status["loaded_sessions"] <= 8
