"""Authentication, validation and the session endpoints (no model involved)."""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from seawall.server.app import create_app
from tests.test_server.conftest import TOKEN, create_session


# --- authentication ----------------------------------------------------------------------------


def test_health_needs_no_token_but_everything_else_does(make_client, config):
    with make_client() as client:
        anonymous = TestClient(client.app)
        assert anonymous.get("/healthz").json()["status"] == "ok"
        refused = anonymous.get("/v1/sessions")
        assert refused.status_code == 401
        assert refused.headers["www-authenticate"] == "Bearer"
        assert refused.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("header", [
    "Bearer wrong-token-0123456789abcdef", "Bearer", "Basic " + TOKEN, TOKEN, "bearer ",
])
def test_wrong_credentials_are_refused(client, header):
    assert client.get("/v1/sessions", headers={"Authorization": header}).status_code == 401


def test_the_scheme_name_is_case_insensitive(client):
    assert client.get("/v1/sessions", headers={"Authorization": f"BEARER {TOKEN}"}).status_code == 200


def test_no_token_mode_serves_loopback_only_and_never_a_browser(make_client):
    with make_client(token=None, allow_no_auth=True) as client:
        bare = TestClient(client.app, base_url="http://127.0.0.1:8765")
        assert bare.get("/v1/sessions").status_code == 200
        assert TestClient(client.app, base_url="http://localhost:8765").get("/v1/sessions").status_code == 200
        assert TestClient(client.app, base_url="http://[::1]:8765").get("/v1/sessions").status_code == 200

        # DNS rebinding: a hostile page resolves its own name to 127.0.0.1
        rebinding = TestClient(client.app, base_url="http://evil.example:8765").get("/v1/sessions")
        assert rebinding.status_code == 403 and rebinding.json()["error"]["code"] == "forbidden_host"

        # a cross-site request from a page in the user's browser
        cross_site = bare.get("/v1/sessions", headers={"Origin": "https://evil.example"})
        assert cross_site.status_code == 403 and cross_site.json()["error"]["code"] == "forbidden_origin"


def test_the_app_will_not_build_without_a_token_on_a_public_address(config):
    from dataclasses import replace

    from seawall.server.config import ConfigError

    with pytest.raises(ConfigError):
        create_app(replace(config, token=None, allow_no_auth=True, host="0.0.0.0"))


def test_unknown_routes_and_methods_get_json_errors(client):
    missing = client.get("/v1/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    wrong_method = client.put("/v1/sessions")
    assert wrong_method.status_code == 405 and wrong_method.json()["error"]["code"] == "method_not_allowed"


# --- creating sessions -------------------------------------------------------------------------


def test_a_session_can_be_created_with_nothing_but_defaults(client, workspace):
    assert client.post("/v1/sessions").status_code == 201  # no body at all
    created = client.post("/v1/sessions", json={})
    assert created.status_code == 201
    body = created.json()
    assert body["state"] == "idle"
    assert body["cwd"] == str(workspace.resolve())
    assert body["spec"]["permission_mode"] == "default"  # never inherited from the operator's settings
    assert created.headers["location"] == f"/v1/sessions/{body['id']}"
    assert body["usage"]["input_tokens"] == 0 and body["cost_usd"] == 0


def test_the_mode_in_the_operators_settings_is_not_inherited(client, tmp_path):
    """A settings.json that says full_auto must not turn every API session into full_auto."""
    config_dir = os.environ["SEAWALL_CONFIG_DIR"]
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "settings.json"), "w") as handle:
        handle.write('{"permission": {"mode": "full_auto"}}')
    assert create_session(client)["spec"]["permission_mode"] == "default"


def test_cwd_may_be_a_subdirectory_of_the_workspace(client, workspace):
    (workspace / "project" / "src").mkdir(parents=True)
    body = create_session(client, cwd="project/src")
    assert body["cwd"] == str((workspace / "project" / "src").resolve())


@pytest.mark.parametrize("cwd", ["..", "../..", "project/../..", "/", "/etc", "~"])
def test_cwd_cannot_leave_the_workspace_or_be_missing(client, workspace, cwd):
    (workspace / "project").mkdir()
    response = client.post("/v1/sessions", json={"cwd": cwd})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"cwd_outside_workspace", "cwd_not_found"}


def test_a_symlink_out_of_the_workspace_does_not_count_as_inside(client, workspace, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (workspace / "link").symlink_to(outside)
    response = client.post("/v1/sessions", json={"cwd": "link"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "cwd_outside_workspace"


def test_a_cwd_with_a_null_byte_is_a_client_error_not_a_crash(client):
    response = client.post("/v1/sessions", json={"cwd": "a\u0000b"})
    assert response.status_code == 422


def test_a_file_is_not_a_cwd(client, workspace):
    (workspace / "file.txt").write_text("x")
    response = client.post("/v1/sessions", json={"cwd": "file.txt"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "cwd_not_found"


@pytest.mark.parametrize("field", ["api_key", "base_url", "sandbox", "env"])
def test_a_client_cannot_supply_credentials_or_endpoints(client, field):
    response = client.post("/v1/sessions", json={field: "x"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("spec", [
    {"max_turns": 0}, {"max_budget_usd": -1}, {"max_seconds": 0}, {"permission_mode": "bypass"},
    {"allowed_tools": ["bash, rm"]}, {"allowed_tools": [""]}, {"title": "x" * 201}, {"model": 7},
])
def test_invalid_specs_are_rejected_with_the_field_named(client, spec):
    response = client.post("/v1/sessions", json=spec)
    assert response.status_code == 422
    assert next(iter(spec)) in response.json()["error"]["message"]


def test_unattended_modes_are_off_unless_the_operator_allows_them(make_client):
    with make_client() as client:
        for spec in ({"permission_mode": "full_auto"}, {"allowed_tools": ["bash"]}):
            response = client.post("/v1/sessions", json=spec)
            assert response.status_code == 403 and response.json()["error"]["code"] == "unattended_disabled"
        assert client.post("/v1/sessions", json={"permission_mode": "plan"}).status_code == 201
        assert client.post("/v1/sessions", json={"disallowed_tools": ["bash"]}).status_code == 201
    with make_client(allow_full_auto=True) as client:
        assert client.post("/v1/sessions", json={"permission_mode": "full_auto"}).status_code == 201
        assert client.post("/v1/sessions", json={"allowed_tools": ["bash"]}).status_code == 201


def test_server_caps_bound_what_a_client_may_ask_for(make_client):
    with make_client(max_budget_usd=2.0, max_turns=10, max_total_tokens=50_000, max_seconds=120) as client:
        defaulted = create_session(client)["spec"]
        assert defaulted["max_budget_usd"] == 2.0 and defaulted["max_turns"] == 10
        assert defaulted["max_total_tokens"] == 50_000 and defaulted["max_seconds"] == 120
        lower = create_session(client, max_budget_usd=0.5, max_turns=3)["spec"]
        assert lower["max_budget_usd"] == 0.5 and lower["max_turns"] == 3
        too_high = client.post("/v1/sessions", json={"max_budget_usd": 5})
        assert too_high.status_code == 422 and too_high.json()["error"]["code"] == "limit_too_high"
        assert "2" in too_high.json()["error"]["message"]


def test_without_caps_a_client_limit_is_kept_as_given(client):
    assert create_session(client, max_budget_usd=123.0)["spec"]["max_budget_usd"] == 123.0


def test_unknown_tool_names_are_reported_as_warnings(make_client):
    with make_client(allow_full_auto=True) as client:
        body = create_session(client, disallowed_tools=["bsh", "bash", "mcp__x__y"], allowed_tools=["raed_file"])
        text = " ".join(body["warnings"])
        assert "bsh" in text and "raed_file" in text
        assert "mcp__x__y" not in text and "'bash'" not in text


def test_the_session_limit(make_client):
    with make_client(max_sessions=2) as client:
        create_session(client)
        create_session(client)
        response = client.post("/v1/sessions", json={})
        assert response.status_code == 403 and response.json()["error"]["code"] == "session_limit"


# --- bodies ------------------------------------------------------------------------------------


def test_malformed_bodies(client):
    assert client.post("/v1/sessions", content="{nope", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/v1/sessions", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/v1/sessions", json=[1, 2]).status_code == 422  # valid JSON, wrong shape
    assert client.post("/v1/sessions", json="text").status_code == 422


def test_oversized_bodies_are_refused_before_being_read_in_full(make_client):
    with make_client(max_message_chars=100) as client:
        big = {"title": "x" * 200_000}
        response = client.post("/v1/sessions", json=big)
        assert response.status_code == 413 and response.json()["error"]["code"] == "body_too_large"


# --- reading and deleting ----------------------------------------------------------------------


def test_get_list_and_paginate(client):
    ids = [create_session(client, title=f"s{n}")["id"] for n in range(5)]
    got = client.get(f"/v1/sessions/{ids[0]}").json()
    assert got["id"] == ids[0] and got["title"] == "s0"

    listing = client.get("/v1/sessions?limit=2").json()
    assert listing["total"] == 5 and len(listing["sessions"]) == 2
    rest = client.get("/v1/sessions?limit=200&offset=2").json()
    assert len(rest["sessions"]) == 3
    assert {s["id"] for s in listing["sessions"]} | {s["id"] for s in rest["sessions"]} == set(ids)


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=x", "offset=-1"])
def test_bad_paging_parameters(client, query):
    assert client.get(f"/v1/sessions?{query}").status_code == 400


@pytest.mark.parametrize("session_id", ["000000000000", "short", "../../etc/passwd", "ZZZZZZZZZZZZ", "a" * 40])
def test_unknown_or_malformed_ids_are_404(client, session_id):
    for path in ("", "/messages", "/runs", "/approvals", "/audit"):
        assert client.get(f"/v1/sessions/{session_id}{path}").status_code == 404
    assert client.delete(f"/v1/sessions/{session_id}").status_code == 404
    assert client.post(f"/v1/sessions/{session_id}/messages", json={"text": "x"}).status_code == 404
    assert client.post(f"/v1/sessions/{session_id}/interrupt").status_code == 404
    assert client.get(f"/v1/sessions/{session_id}/events").status_code == 404


def test_delete_removes_the_session_but_not_its_audit_log(client, workspace, provider):
    from tests.test_server.conftest import run_message

    session = create_session(client)
    run_message(client, session["id"])
    audit_dir = os.path.join(os.environ["SEAWALL_DATA_DIR"], "audit")
    log = os.path.join(audit_dir, f"{session['id']}.jsonl")
    assert os.path.exists(log)

    assert client.delete(f"/v1/sessions/{session['id']}").status_code == 204
    assert client.get(f"/v1/sessions/{session['id']}").status_code == 404
    assert client.get("/v1/sessions").json()["total"] == 0
    assert os.path.exists(log)  # the record of what the agent did outlives the session


def test_status_reports_load_and_limits(make_client):
    with make_client(max_concurrent_runs=3, max_budget_usd=1.5) as client:
        status = client.get("/v1/status").json()
        assert status["running"] == 0 and status["queued"] == 0 and status["loaded_sessions"] == 0
        assert status["limits"]["max_concurrent_runs"] == 3 and status["limits"]["max_budget_usd"] == 1.5
        assert status["full_auto_allowed"] is False


def test_responses_are_marked_private(client):
    session = create_session(client)
    for response in (
        client.get("/healthz"),
        client.get("/v1/sessions"),
        client.get(f"/v1/sessions/{session['id']}"),
        client.get("/v1/nope"),
        TestClient(client.app).get("/v1/sessions"),  # even a refusal
    ):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    from tests.test_server.conftest import run_message

    result = run_message(client, session["id"])
    stream = client.get(f"/v1/sessions/{session['id']}/events?until_run={result['run_id']}")
    assert stream.headers["cache-control"] == "no-store"
