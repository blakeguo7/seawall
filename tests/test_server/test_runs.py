"""Messages, runs and everything that can happen to them."""

from __future__ import annotations

import threading
import time


from seawall.api.usage import UsageSnapshot
from tests.fakes import GeneratedClient, ScriptedClient, text_message, tool_message
from tests.test_server.conftest import (
    BrokenClient,
    GatedClient,
    create_session,
    parse_sse,
    run_message,
    wait_for,
)


def events_of(client, session_id: str, run_id: str) -> list[dict]:
    """The events of a run that has ended (or ends on its own)."""
    response = client.get(f"/v1/sessions/{session_id}/events?until_run={run_id}")
    assert response.status_code == 200
    return parse_sse(response.text)


def kinds(events: list[dict]) -> list[str]:
    return [event["event"] for event in events]


def send(client, session_id: str, text: str = "go") -> dict:
    response = client.post(f"/v1/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 202, response.text
    return response.json()


# --- the shape of a run ------------------------------------------------------------------------


def test_a_run_with_a_tool_call_streams_every_step_in_order(client, provider, workspace):
    (workspace / "notes.txt").write_text("remember the milk")
    session = create_session(client)
    provider.script(
        session["id"],
        ScriptedClient(tool_message(("read_file", {"path": "notes.txt"})), text_message("It says: remember the milk")),
    )

    result = run_message(client, session["id"], "what do my notes say?")
    events = events_of(client, session["id"], result["run_id"])

    assert kinds(events)[0] == "run.started" and kinds(events)[-1] == "run.finished"
    middle = [k for k in kinds(events) if k != "assistant.delta"]
    assert middle == [
        "run.started", "assistant.message", "tool.started", "tool.completed", "assistant.message", "run.finished",
    ]
    started = next(e for e in events if e["event"] == "tool.started")
    assert started["data"]["data"]["tool_name"] == "read_file"
    completed = next(e for e in events if e["event"] == "tool.completed")
    assert "remember the milk" in completed["data"]["data"]["output"]
    assert completed["data"]["data"]["denied"] is False

    # numbers are consecutive, the SSE id is the number, and every event names its run
    seqs = [e["data"]["seq"] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert [e["id"] for e in events] == [str(n) for n in seqs]
    assert {e["data"]["data"]["run_id"] for e in events} == {result["run_id"]}

    assert result["text"] == "It says: remember the milk"
    assert result["stop_reason"] == "completed" and result["turns"] == 2
    assert result["usage"]["input_tokens"] == 20 and result["usage"]["output_tokens"] == 10


def test_run_started_carries_the_message_and_run_finished_the_outcome(client, provider):
    session = create_session(client)
    result = run_message(client, session["id"], "please answer")
    events = events_of(client, session["id"], result["run_id"])
    assert events[0]["data"]["data"]["text"] == "please answer"
    finished = events[-1]["data"]["data"]
    assert finished["stop_reason"] == "completed" and finished["turns"] == 1
    assert finished["usage"]["input_tokens"] == 10 and finished["duration_seconds"] >= 0
    assert "text" not in finished  # the answer is in assistant.message; not sent twice


def test_the_conversation_continues_across_messages(client, provider):
    session = create_session(client)
    answers = ["Nice to meet you, Ada.", "Your name is Ada."]
    model = GeneratedClient(lambda n: text_message(answers[n - 1]))
    provider.script(session["id"], model)

    run_message(client, session["id"], "My name is Ada.")
    second = run_message(client, session["id"], "What is my name?")

    assert second["text"] == "Your name is Ada."
    sent = model.sent[1]  # the conversation as the model saw it on its second call
    assert [m.role for m in sent] == ["user", "assistant", "user"]
    assert "My name is Ada." in sent[0].text and "What is my name?" in sent[2].text

    conversation = client.get(f"/v1/sessions/{session['id']}/messages").json()["messages"]
    assert [m["role"] for m in conversation] == ["user", "assistant", "user", "assistant"]


def test_the_session_row_tracks_usage_and_the_last_stop(client, provider):
    session = create_session(client)
    provider.script(session["id"], ScriptedClient(text_message("a"), text_message("b")))
    run_message(client, session["id"])
    run_message(client, session["id"])
    row = client.get(f"/v1/sessions/{session['id']}").json()
    assert row["state"] == "idle" and row["last_stop_reason"] == "completed"
    assert row["usage"]["input_tokens"] == 20 and row["usage"]["output_tokens"] == 10
    runs = client.get(f"/v1/sessions/{session['id']}/runs").json()["runs"]
    assert [r["status"] for r in runs] == ["finished", "finished"] and runs[0]["turns"] == 1


# --- the three ways to send ---------------------------------------------------------------------


def test_the_default_answer_is_202_with_the_place_to_follow_the_run(client, provider):
    session = create_session(client)
    accepted = send(client, session["id"])
    assert accepted["events_url"].startswith(f"/v1/sessions/{session['id']}/events?after=0&until_run=")
    stream = client.get(accepted["events_url"])  # follows it to the end
    assert kinds(parse_sse(stream.text))[-1] == "run.finished"
    assert stream.headers["content-type"].startswith("text/event-stream")


def test_a_message_can_ask_for_the_stream_in_the_same_request(client, provider):
    session = create_session(client)
    provider.script(session["id"], ScriptedClient(text_message("streamed")))
    response = client.post(
        f"/v1/sessions/{session['id']}/messages", json={"text": "hi"}, headers={"Accept": "text/event-stream"}
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert kinds(events)[0] == "run.started" and kinds(events)[-1] == "run.finished"
    assert any(e["event"] == "assistant.delta" and e["data"]["data"]["text"] == "streamed" for e in events)


def test_a_second_run_does_not_replay_the_first(client, provider):
    session = create_session(client)
    first = run_message(client, session["id"], "one")
    accepted = send(client, session["id"], "two")
    events = parse_sse(client.get(accepted["events_url"]).text)
    assert {e["data"]["data"]["run_id"] for e in events} == {accepted["run_id"]}
    assert first["run_id"] != accepted["run_id"]


# --- refusing messages -------------------------------------------------------------------------


def test_message_bodies_are_validated(make_client):
    with make_client(max_message_chars=50) as client:
        session = create_session(client)
        url = f"/v1/sessions/{session['id']}/messages"
        assert client.post(url, json={"text": ""}).status_code == 422
        assert client.post(url, json={}).status_code == 422
        assert client.post(url, json={"text": "x", "model": "other"}).status_code == 422
        assert client.post(url).status_code == 400  # no body
        too_long = client.post(url, json={"text": "x" * 51})
        assert too_long.status_code == 413 and too_long.json()["error"]["code"] == "message_too_long"
        assert client.post(url, json={"text": "x" * 50}, params={"wait": 1}).status_code == 200


def test_a_session_runs_one_message_at_a_time(client, provider):
    session = create_session(client)
    model = GatedClient()
    provider.script(session["id"], model)
    first = send(client, session["id"])
    wait_for(model.started.is_set, what="the first run to reach the model")

    refused = client.post(f"/v1/sessions/{session['id']}/messages", json={"text": "again"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "busy"
    assert client.delete(f"/v1/sessions/{session['id']}").status_code == 409
    assert client.get(f"/v1/sessions/{session['id']}").json()["state"] == "running"

    model.release()
    events = events_of(client, session["id"], first["run_id"])
    assert kinds(events)[-1] == "run.finished"
    # the moment run.finished is visible, the next message is accepted
    assert client.post(f"/v1/sessions/{session['id']}/messages", json={"text": "now"}).status_code == 202


# --- concurrency -------------------------------------------------------------------------------


def test_runs_proceed_in_parallel_up_to_the_cap_and_then_wait(make_client, provider):
    with make_client(max_concurrent_runs=2, max_queued_runs=4, max_loaded_sessions=4) as client:
        sessions = [create_session(client)["id"] for _ in range(3)]
        models = [GatedClient() for _ in sessions]
        for sid, model in zip(sessions, models):
            provider.script(sid, model)
        accepted = [send(client, sid) for sid in sessions]

        wait_for(lambda: models[0].started.is_set() and models[1].started.is_set(), what="two runs in parallel")
        time.sleep(0.2)
        assert not models[2].started.is_set()  # the third waits for a slot
        status = client.get("/v1/status").json()
        assert status["running"] == 2 and status["queued"] == 1
        assert client.get(f"/v1/sessions/{sessions[2]}").json()["state"] == "queued"

        models[0].release()
        wait_for(models[2].started.is_set, what="the queued run to take the freed slot")
        for model in models[1:]:
            model.release()
        for sid, run in zip(sessions, accepted):
            assert kinds(events_of(client, sid, run["run_id"]))[-1] == "run.finished"
        final = client.get("/v1/status").json()
        assert final["running"] == 0 and final["queued"] == 0


def test_a_full_queue_answers_429_with_retry_after(make_client, provider):
    with make_client(max_concurrent_runs=1, max_queued_runs=1, max_loaded_sessions=1) as client:
        sessions = [create_session(client)["id"] for _ in range(3)]
        models = [GatedClient() for _ in sessions]
        for sid, model in zip(sessions, models):
            provider.script(sid, model)
        first, second = send(client, sessions[0]), send(client, sessions[1])
        wait_for(models[0].started.is_set)

        refused = client.post(f"/v1/sessions/{sessions[2]}/messages", json={"text": "x"})
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "overloaded" and int(refused.headers["retry-after"]) >= 1
        assert client.get(f"/v1/sessions/{sessions[2]}").json()["state"] == "idle"  # nothing was left half-registered

        for model in models:
            model.release()
        for sid, run in zip(sessions, (first, second)):
            assert kinds(events_of(client, sid, run["run_id"]))[-1] == "run.finished"
        # the refused session is fine afterwards
        assert run_message(client, sessions[2])["stop_reason"] == "completed"


def test_with_no_queue_a_busy_server_refuses_at_once(make_client, provider):
    with make_client(max_concurrent_runs=1, max_queued_runs=0, max_loaded_sessions=1) as client:
        a, b = create_session(client)["id"], create_session(client)["id"]
        model = GatedClient()
        provider.script(a, model)
        send(client, a)
        wait_for(model.started.is_set)
        assert client.post(f"/v1/sessions/{b}/messages", json={"text": "x"}).status_code == 429
        model.release()


# --- interrupting ------------------------------------------------------------------------------


def test_interrupting_a_run_ends_it_and_frees_the_session(client, provider):
    session = create_session(client)
    model = GatedClient()
    provider.script(session["id"], model)
    accepted = send(client, session["id"])
    wait_for(model.started.is_set)

    assert client.post(f"/v1/sessions/{session['id']}/interrupt").json() == {"interrupted": True}
    events = events_of(client, session["id"], accepted["run_id"])
    finished = events[-1]["data"]["data"]
    assert finished["stop_reason"] == "interrupted" and "interrupted by request" in finished["detail"]
    assert model.cancelled.is_set()  # the call to the model really was cancelled

    row = client.get(f"/v1/sessions/{session['id']}").json()
    assert row["state"] == "idle" and row["last_stop_reason"] == "interrupted"
    assert client.get("/v1/status").json()["running"] == 0
    # and the session works again (the same model, now allowed to answer)
    model.release()
    again = run_message(client, session["id"])
    assert again["stop_reason"] == "completed" and again["text"] == "finally done"


def test_interrupting_an_idle_session_is_not_an_error(client):
    session = create_session(client)
    assert client.post(f"/v1/sessions/{session['id']}/interrupt").json() == {"interrupted": False}


def test_interrupting_a_run_that_is_still_queued(make_client, provider):
    with make_client(max_concurrent_runs=1, max_queued_runs=2, max_loaded_sessions=1) as client:
        a, b = create_session(client)["id"], create_session(client)["id"]
        model_a, model_b = GatedClient(), GatedClient()
        provider.script(a, model_a)
        provider.script(b, model_b)
        send(client, a)
        wait_for(model_a.started.is_set)
        queued = send(client, b)

        assert client.post(f"/v1/sessions/{b}/interrupt").json() == {"interrupted": True}
        finished = events_of(client, b, queued["run_id"])[-1]["data"]["data"]
        assert finished["stop_reason"] == "interrupted"
        assert not model_b.started.is_set()  # it never reached the model
        model_a.release()
        assert client.get("/v1/status").json()["queued"] == 0


def test_a_run_that_outlives_the_server_time_limit_is_stopped(make_client, provider):
    with make_client(run_timeout_seconds=0.3) as client:
        session = create_session(client)
        model = GatedClient()
        provider.script(session["id"], model)
        accepted = send(client, session["id"])
        finished = events_of(client, session["id"], accepted["run_id"])[-1]["data"]["data"]
        assert finished["stop_reason"] == "time_limit" and "0.3s" in finished["detail"]
        assert model.cancelled.is_set()


# --- limits and failures -----------------------------------------------------------------------


def test_the_sessions_turn_limit_is_enforced(client, provider):
    session = create_session(client, max_turns=2)
    provider.script(
        session["id"],
        GeneratedClient(lambda n: tool_message(("glob", {"pattern": f"*{n}"}), turn=n)),
    )
    result = run_message(client, session["id"])
    assert result["stop_reason"] == "max_turns"
    assert result["turns"] == 2


def test_the_sessions_token_limit_is_enforced(client, provider):
    session = create_session(client, max_total_tokens=100)
    provider.script(
        session["id"],
        GeneratedClient(
            lambda n: tool_message(("glob", {"pattern": f"*{n}"}), turn=n),
            usage=UsageSnapshot(input_tokens=80, output_tokens=40),
        ),
    )
    result = run_message(client, session["id"])
    assert result["stop_reason"] == "token_limit"
    assert result["turns"] == 1


def test_a_server_cap_applies_to_sessions_that_asked_for_nothing(make_client, provider):
    with make_client(max_turns=1) as client:
        session = create_session(client)
        provider.script(session["id"], GeneratedClient(lambda n: tool_message(("glob", {"pattern": "*"}), turn=n)))
        assert run_message(client, session["id"])["stop_reason"] == "max_turns"


def test_a_model_that_raises_ends_the_run_and_the_server_carries_on(client, provider):
    session = create_session(client)
    provider.script(session["id"], BrokenClient(RuntimeError("the model exploded")))
    result = run_message(client, session["id"])
    assert result["stop_reason"] == "error"
    assert "exploded" in result["detail"]
    other = create_session(client)
    assert run_message(client, other["id"])["stop_reason"] == "completed"
    assert client.get(f"/v1/sessions/{session['id']}").json()["state"] == "idle"


def test_a_server_with_no_model_credentials_reports_it_and_stays_up(config, tmp_path):
    from dataclasses import replace

    from starlette.testclient import TestClient

    from seawall.server.app import create_app
    from tests.test_server.conftest import AUTH

    app = create_app(replace(config, db_path=tmp_path / "nokey.db"))  # no client factory: real provider lookup
    with TestClient(app, headers=AUTH, raise_server_exceptions=False) as client:
        session = create_session(client)
        result = run_message(client, session["id"])
        assert result["stop_reason"] == "error" and "credentials" in result["detail"]
        assert client.get("/healthz").json()["status"] == "ok"  # SystemExit from the runtime did not take the server down


def test_tools_run_in_the_sessions_directory(make_client, provider, workspace):
    (workspace / "sub").mkdir()
    with make_client(allow_full_auto=True) as client:
        session = create_session(client, cwd="sub", allowed_tools=["write_file"])
        provider.script(
            session["id"],
            ScriptedClient(tool_message(("write_file", {"path": "made.txt", "content": "hi"})), text_message("ok")),
        )
        assert run_message(client, session["id"])["stop_reason"] == "completed"
        assert (workspace / "sub" / "made.txt").read_text() == "hi"
        assert not (workspace / "made.txt").exists()


def test_concurrent_sessions_do_not_mix_their_events_or_answers(make_client, provider):
    with make_client(max_concurrent_runs=4, max_queued_runs=32, max_loaded_sessions=8) as client:
        sessions = [create_session(client, title=f"s{n}")["id"] for n in range(8)]
        for n, sid in enumerate(sessions):
            provider.script(sid, ScriptedClient(text_message(f"answer {n}")))
        results: dict[str, dict] = {}

        def work(n: int, sid: str) -> None:
            results[sid] = run_message(client, sid, f"question {n}")

        threads = [threading.Thread(target=work, args=(n, sid)) for n, sid in enumerate(sessions)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        assert len({r["run_id"] for r in results.values()}) == 8
        for n, sid in enumerate(sessions):
            assert results[sid]["text"] == f"answer {n}"
            events = events_of(client, sid, results[sid]["run_id"])
            assert {e["data"]["session_id"] for e in events} == {sid}
            assert events[0]["data"]["data"]["text"] == f"question {n}"


def test_interrupting_a_running_command_kills_it(make_client, provider, workspace):
    """The model's shell command must not outlive the run that started it."""
    import subprocess

    marker = "sleep 31.4159"
    with make_client(allow_full_auto=True) as client:
        session = create_session(client, permission_mode="full_auto")
        provider.script(
            session["id"], ScriptedClient(tool_message(("bash", {"command": marker})), text_message("never reached"))
        )
        accepted = send(client, session["id"])

        def running() -> bool:
            return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0

        wait_for(running, what="the command to start")
        assert client.post(f"/v1/sessions/{session['id']}/interrupt").json() == {"interrupted": True}
        finished = events_of(client, session["id"], accepted["run_id"])[-1]["data"]["data"]
        assert finished["stop_reason"] == "interrupted"
        wait_for(lambda: not running(), timeout=5, what="the command to be killed")


def test_a_message_starting_with_a_slash_is_text_for_the_model_not_a_command(client, provider):
    """Slash commands change settings on disk; they must not exist for API clients."""
    session = create_session(client)
    model = GeneratedClient(lambda n: text_message("I got it"))
    provider.script(session["id"], model)
    assert run_message(client, session["id"], "/clear")["text"] == "I got it"
    assert model.sent[0][-1].text == "/clear"
    assert run_message(client, session["id"], "/model some-other-model")["stop_reason"] == "completed"
    assert [m.text for m in model.sent[1] if m.role == "user"] == ["/clear", "/model some-other-model"]
