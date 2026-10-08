"""Restarts, crashes, two servers on one database, and runtimes that are evicted and rebuilt."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time

from seawall.api.usage import UsageSnapshot
from seawall.server.store import SessionRow, SessionStore
from tests.fakes import GeneratedClient, ScriptedClient, text_message, tool_message
from tests.test_server.conftest import GatedClient, create_session, parse_sse, run_message, wait_for


def kinds(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


# --- restarts ----------------------------------------------------------------------------------


def test_sessions_and_their_conversations_survive_a_restart(make_client, provider):
    answers = ["Nice to meet you, Ada.", "Your name is Ada."]
    with make_client() as first:
        session = create_session(first, title="persistent")
        provider.script(session["id"], GeneratedClient(lambda n: text_message(answers[0])))
        run_message(first, session["id"], "My name is Ada.")

    model = GeneratedClient(lambda n: text_message(answers[1]))
    provider.clients.clear()
    provider.script(session["id"], model)
    with make_client() as second:
        again = second.get(f"/v1/sessions/{session['id']}").json()
        assert again["title"] == "persistent" and again["state"] == "idle"
        assert again["usage"]["input_tokens"] == 1000  # what the first process's one model call used
        conversation = second.get(f"/v1/sessions/{session['id']}/messages").json()["messages"]
        assert [m["role"] for m in conversation] == ["user", "assistant"]

        result = run_message(second, session["id"], "What is my name?")
        assert result["text"] == "Your name is Ada."
        assert [m.role for m in model.sent[0]] == ["user", "assistant", "user"]  # it remembered
        assert second.get(f"/v1/sessions/{session['id']}").json()["usage"]["input_tokens"] == 2000

        # the first process's events are still there and numbering carries on after them
        everything = parse_sse(second.get(f"/v1/sessions/{session['id']}/events?until_run={result['run_id']}").text)
        numbers = [e["data"]["seq"] for e in everything]
        assert numbers == list(range(1, len(numbers) + 1))
        started = [e for e in everything if e["event"] == "run.started"]
        assert len(started) == 2 and started[1]["data"]["data"]["run_id"] == result["run_id"]
        assert started[0]["data"]["seq"] < started[1]["data"]["seq"]
        assert kinds(everything)[-1] == "run.finished"


def test_the_audit_log_of_a_resumed_session_is_one_unbroken_chain(make_client, provider):
    with make_client() as first:
        session = create_session(first)
        run_message(first, session["id"])
    with make_client() as second:
        run_message(second, session["id"])
        report = second.get(f"/v1/sessions/{session['id']}/audit").json()
    assert report["verified"] is True
    loads = [r for r in report["records"] if r["type"] == "server.session_loaded"]
    assert [r["data"]["resumed"] for r in loads] == [False, True]
    types = [r["type"] for r in report["records"]]
    # the first process closed the session properly; the second still has it open
    assert types.count("session.start") == 2 and types.count("session.end") == 1


def test_a_crash_is_cleaned_up_when_the_server_starts(make_client, provider, config):
    async def leave_wreckage() -> str:
        store = SessionStore(config.db_path)
        await store.open()
        now = time.time()
        sid = "abcdef012345"
        await store.create_session(SessionRow(sid, "was running", str(config.workspace_root), "running", {"permission_mode": "default"}, now, now))
        await store.create_run("deadrun00001", sid, "never finished")
        await store.mark_run_started("deadrun00001")
        await store.append_event(sid, "run.started", {"run_id": "deadrun00001", "text": "never finished"})
        await store.save_approval(
            {"id": "stale1", "session_id": sid, "run_id": "deadrun00001", "tool_name": "bash", "summary": "ls",
             "reason": "r", "risk": "low", "risk_reasons": [], "created_at": now}
        )
        await store.close()
        return sid

    sid = asyncio.run(leave_wreckage())
    with make_client() as client:
        row = client.get(f"/v1/sessions/{sid}").json()
        assert row["state"] == "idle"
        assert client.get(f"/v1/sessions/{sid}/runs").json()["runs"][0]["status"] == "interrupted"
        assert client.get(f"/v1/sessions/{sid}/approvals").json()["approvals"][0]["status"] == "expired"
        # whoever was following the run is told it is over
        events = parse_sse(client.get(f"/v1/sessions/{sid}/events?until_run=deadrun00001").text)
        assert kinds(events) == ["run.started", "run.finished"]
        assert events[-1]["data"]["data"]["stop_reason"] == "interrupted"
        # and the session is usable
        provider.script(sid, ScriptedClient(text_message("back")))
        assert run_message(client, sid)["stop_reason"] == "completed"


# --- two servers, one database -----------------------------------------------------------------


def test_a_session_runs_in_one_server_at_a_time(make_client, provider):
    with make_client(server_id="server-one") as one, make_client(server_id="server-two") as two:
        session = create_session(one)
        model = GatedClient()
        provider.script(session["id"], model)
        accepted = one.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"}).json()
        wait_for(model.started.is_set)

        refused = two.post(f"/v1/sessions/{session['id']}/messages", json={"text": "me too"})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "session_elsewhere"
        assert two.delete(f"/v1/sessions/{session['id']}").status_code == 409
        assert two.get(f"/v1/sessions/{session['id']}").status_code == 200  # reading is fine from anywhere
        assert two.post(f"/v1/sessions/{session['id']}/interrupt").json() == {"interrupted": False}  # not running here

        # the other server can follow the run: its stream reads the shared database
        followed: list[dict] = []
        reader = threading.Thread(
            target=lambda: followed.extend(parse_sse(two.get(accepted["events_url"]).text)), daemon=True
        )
        reader.start()
        time.sleep(0.3)
        model.release()
        reader.join(10)
        assert kinds(followed)[0] == "run.started" and kinds(followed)[-1] == "run.finished"

        # once the run is over the lease is free, and the other server picks the conversation up
        again = run_message(two, session["id"], "your turn")
        assert again["stop_reason"] == "completed"
        conversation = two.get(f"/v1/sessions/{session['id']}/messages").json()["messages"]
        assert [m["role"] for m in conversation] == ["user", "assistant", "user", "assistant"]


def test_the_other_server_cannot_answer_an_approval_it_does_not_hold(make_client, provider):
    with make_client(server_id="server-one") as one, make_client(server_id="server-two") as two:
        session = create_session(one)
        provider.script(
            session["id"],
            ScriptedClient(tool_message(("write_file", {"path": "a.txt", "content": "x"})), text_message("ok")),
        )
        accepted = one.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"}).json()
        request = wait_for(
            lambda: two.get(f"/v1/sessions/{session['id']}/approvals?status=pending").json()["approvals"]
        )[0]  # visible from both
        refused = two.post(f"/v1/sessions/{session['id']}/approvals/{request['id']}", json={"approved": True})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_pending_here"
        assert one.post(f"/v1/sessions/{session['id']}/approvals/{request['id']}", json={"approved": False}).status_code == 200
        assert kinds(parse_sse(one.get(accepted["events_url"]).text))[-1] == "run.finished"


def test_a_lease_left_by_a_dead_server_is_taken_over(make_client, provider, config):
    with make_client() as client:
        session = create_session(client)
        conn = sqlite3.connect(config.db_path)
        conn.execute(
            "UPDATE sessions SET lease_owner = 'ghost', lease_expires_at = ? WHERE id = ?", (time.time() - 10, session["id"])
        )
        conn.commit()
        conn.close()
        assert run_message(client, session["id"])["stop_reason"] == "completed"


def test_a_run_that_loses_its_lease_stops_instead_of_running_twice(make_client, provider, config):
    with make_client(lease_seconds=0.3) as client:
        session = create_session(client)
        model = GatedClient()
        provider.script(session["id"], model)
        accepted = client.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"}).json()
        wait_for(model.started.is_set)
        # another server steals it (as if this one had been paused for longer than the lease)
        conn = sqlite3.connect(config.db_path)
        conn.execute(
            "UPDATE sessions SET lease_owner = 'thief', lease_expires_at = ? WHERE id = ?", (time.time() + 600, session["id"])
        )
        conn.commit()
        conn.close()
        events = parse_sse(client.get(accepted["events_url"]).text)
        finished = events[-1]["data"]["data"]
        assert finished["stop_reason"] == "error" and "lease" in finished["detail"]
        assert model.cancelled.is_set()


def test_a_healthy_run_keeps_its_lease_past_the_lease_time(make_client, provider):
    with make_client(server_id="server-one", lease_seconds=0.6) as one, make_client(server_id="server-two", lease_seconds=0.6) as two:
        session = create_session(one)
        model = GatedClient()
        provider.script(session["id"], model)
        accepted = one.post(f"/v1/sessions/{session['id']}/messages", json={"text": "go"}).json()
        wait_for(model.started.is_set)
        time.sleep(1.5)  # two and a half lease periods
        assert two.post(f"/v1/sessions/{session['id']}/messages", json={"text": "x"}).status_code == 409
        model.release()
        assert kinds(parse_sse(one.get(accepted["events_url"]).text))[-1] == "run.finished"


# --- runtimes that come and go -----------------------------------------------------------------


def test_an_evicted_session_comes_back_with_its_history_usage_and_limits(make_client, provider):
    with make_client(max_concurrent_runs=1, max_loaded_sessions=1) as client:
        a = create_session(client, max_total_tokens=40)["id"]
        b = create_session(client)["id"]

        def a_turns(n: int):
            return tool_message(("glob", {"pattern": f"*{n}"}), turn=n) if n % 2 else text_message(f"a done {n}")

        model_a = GeneratedClient(a_turns, usage=UsageSnapshot(input_tokens=10, output_tokens=5))
        provider.script(a, model_a)
        provider.script(b, ScriptedClient(text_message("b says hi")))

        first = run_message(client, a, "first")  # tool call then answer: 2 turns, 30 tokens
        assert first["stop_reason"] == "completed"
        assert client.get("/v1/status").json()["loaded_sessions"] == 1
        run_message(client, b, "hello")  # evicts A's runtime (the cache holds one)
        assert client.get("/v1/status").json()["loaded_sessions"] == 1
        assert provider.built.count(a) == 1

        second = run_message(client, a, "second")  # A is rebuilt from its snapshot
        assert provider.built.count(a) == 2
        sent = model_a.sent[2]  # the model's first call of the second message
        assert [m.role for m in sent][:3] == ["user", "assistant", "user"] and sent[-1].text == "second"
        # the 30 tokens already used count against its limit of 40, even though the runtime is new
        assert second["stop_reason"] == "token_limit"
        row = client.get(f"/v1/sessions/{a}").json()
        assert row["usage"]["input_tokens"] == 30 and row["usage"]["output_tokens"] == 15


def test_closing_the_server_closes_the_cached_runtimes_in_the_audit_log(make_client, provider, config):
    with make_client() as client:
        session = create_session(client)
        run_message(client, session["id"])
    log = os.path.join(os.environ["SEAWALL_DATA_DIR"], "audit", f"{session['id']}.jsonl")
    last = open(log).read().strip().splitlines()[-1]
    assert '"type":"session.end"' in last.replace(" ", "")


def test_deleting_a_session_with_a_cached_runtime_releases_it(make_client, provider):
    with make_client() as client:
        session = create_session(client)
        run_message(client, session["id"])
        assert client.get("/v1/status").json()["loaded_sessions"] == 1
        assert client.delete(f"/v1/sessions/{session['id']}").status_code == 204
        assert client.get("/v1/status").json()["loaded_sessions"] == 0


# --- the audit endpoint ------------------------------------------------------------------------


def audit_url(session_id: str, query: str = "") -> str:
    return f"/v1/sessions/{session_id}/audit{query}"


def test_audit_filters(make_client, provider):
    with make_client(allow_full_auto=True) as client:
        session = create_session(client, permission_mode="full_auto")
        provider.script(
            session["id"],
            ScriptedClient(
                tool_message(("glob", {"pattern": "*"}), ("bash", {"command": "rm -rf ~"})), text_message("done")
            ),
        )
        run_message(client, session["id"])

        everything = client.get(audit_url(session["id"])).json()
        assert everything["verified"] is True and everything["total"] == len(everything["records"])
        denied = client.get(audit_url(session["id"], "?decision=deny")).json()["records"]
        assert denied and all(r["data"]["decision"] == "deny" for r in denied)
        assert any(r["data"]["tool"] == "bash" for r in denied)
        only_glob = client.get(audit_url(session["id"], "?tool=glob")).json()["records"]
        assert only_glob and all(r["data"]["tool"] == "glob" for r in only_glob)
        critical = client.get(audit_url(session["id"], "?min_risk=critical")).json()["records"]
        assert critical and all(r["data"]["risk"] == "critical" for r in critical)
        newest = client.get(audit_url(session["id"], "?limit=2")).json()["records"]
        assert len(newest) == 2 and newest[-1]["type"] == everything["records"][-1]["type"]
        assert client.get(audit_url(session["id"], "?min_risk=extreme")).status_code == 422
        assert client.get(audit_url(session["id"], "?limit=0")).status_code == 400


def test_a_tampered_audit_log_is_reported(make_client, provider):
    with make_client() as client:
        session = create_session(client)
        run_message(client, session["id"])
        path = os.path.join(os.environ["SEAWALL_DATA_DIR"], "audit", f"{session['id']}.jsonl")
        text = open(path).read()
        assert "hello from the model" not in text
        with open(path, "w") as handle:
            handle.write(text.replace('"decision":"allow"', '"decision":"deny"', 1).replace('"cwd"', '"cwdx"', 1))
        report = client.get(audit_url(session["id"])).json()
        assert report["verified"] is False and report["error"]


def test_a_session_without_a_log_says_so(make_client, provider):
    with make_client() as client:
        session = create_session(client)
        report = client.get(audit_url(session["id"])).json()
        assert report["verified"] is None and report["records"] == []
