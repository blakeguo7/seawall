"""Tests for the ``seawall audit`` commands."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from seawall.audit import audit_dir, open_session_audit
from seawall.cli import app
from seawall.config.settings import AuditSettings

runner = CliRunner()


@pytest.fixture
def session_log():
    """A finished session with one allowed call and one refused call."""
    log = open_session_audit(AuditSettings(), "abc123def456")
    log.record("session.start", cwd="/work/proj", permission_mode="default", model="m")
    log.record(
        "tool.decision", call_id="t1", tool="bash", input={"command": "pytest -q"},
        decision="allow", source="policy", reason="read-only", risk="low", mode="default",
    )
    log.record("tool.result", call_id="t1", tool="bash", status="ok", duration_ms=1200)
    log.record(
        "tool.decision", call_id="t2", tool="bash", input={"command": "rm -rf build"},
        decision="deny", source="approval", reason="needs confirmation", risk="high", mode="default",
        approval={"outcome": "unavailable", "decided_by": "no-approver", "note": ""},
    )
    log.record("session.end")
    log.close()
    return audit_dir(AuditSettings()) / "abc123def456.jsonl"


def test_list_shows_sessions_with_their_chain_status(session_log) -> None:
    result = runner.invoke(app, ["audit", "list"])
    assert result.exit_code == 0
    assert "abc123def456" in result.output
    assert "default" in result.output
    assert "5 records" in result.output
    assert "1 denied" in result.output
    assert "[ok]" in result.output
    assert "/work/proj" in result.output


def test_list_with_no_logs() -> None:
    result = runner.invoke(app, ["audit", "list"])
    assert result.exit_code == 0
    assert "No audit logs" in result.output


def test_show_prints_a_readable_timeline(session_log) -> None:
    result = runner.invoke(app, ["audit", "show", "abc123"])
    assert result.exit_code == 0
    assert "session started" in result.output
    assert "ALLOW bash [low] pytest -q" in result.output
    assert "DENY  bash [high] rm -rf build" in result.output
    assert "needs confirmation" in result.output
    assert "session ended" in result.output


def test_show_filters(session_log) -> None:
    denied = runner.invoke(app, ["audit", "show", "abc123", "--decision", "deny"])
    assert "rm -rf build" in denied.output and "pytest" not in denied.output

    risky = runner.invoke(app, ["audit", "show", "abc123", "--min-risk", "high"])
    assert "rm -rf build" in risky.output and "pytest" not in risky.output

    by_tool = runner.invoke(app, ["audit", "show", "abc123", "--tool", "BASH", "--decision", "allow"])
    assert "pytest" in by_tool.output and "rm -rf" not in by_tool.output


def test_show_json_prints_raw_records(session_log) -> None:
    result = runner.invoke(app, ["audit", "show", "abc123", "--json"])
    records = [json.loads(line) for line in result.output.splitlines()]
    assert [r["type"] for r in records][0] == "session.start"
    assert all("hash" in r for r in records)


def test_show_rejects_a_bad_risk_level(session_log) -> None:
    result = runner.invoke(app, ["audit", "show", "abc123", "--min-risk", "severe"])
    assert result.exit_code == 2


def test_unknown_session_exits_2() -> None:
    assert runner.invoke(app, ["audit", "show", "nope"]).exit_code == 2
    assert runner.invoke(app, ["audit", "verify", "nope"]).exit_code == 2


def test_verify_ok(session_log) -> None:
    result = runner.invoke(app, ["audit", "verify", "abc123"])
    assert result.exit_code == 0
    assert "OK: 5 records, hash-chained" in result.output
    assert "not signed" in result.output


def test_verify_catches_tampering_and_exits_1(session_log) -> None:
    lines = session_log.read_text().splitlines()
    lines[3] = lines[3].replace('"deny"', '"allow"')
    session_log.write_text("\n".join(lines) + "\n")

    result = runner.invoke(app, ["audit", "list"])
    assert "[BROKEN]" in result.output

    result = runner.invoke(app, ["audit", "verify", "abc123"])
    assert result.exit_code == 1
    assert "FAILED at line 4" in result.output


def test_verify_checks_the_recorded_head(session_log) -> None:
    head = json.loads(session_log.read_text().splitlines()[-1])["hash"]
    assert runner.invoke(app, ["audit", "verify", "abc123", "--head", head]).exit_code == 0
    assert runner.invoke(app, ["audit", "verify", "abc123", "--head", "0" * 64]).exit_code == 1


def test_verify_signed_log_with_key(monkeypatch) -> None:
    monkeypatch.setenv("SEAWALL_AUDIT_KEY", "s3cret")
    log = open_session_audit(AuditSettings(), "signed1")
    log.record("session.start")
    log.record("session.end")
    log.close()

    ok = runner.invoke(app, ["audit", "verify", "signed1"])
    assert ok.exit_code == 0 and "HMAC-signed" in ok.output

    monkeypatch.setenv("SEAWALL_AUDIT_KEY", "wrong")
    assert runner.invoke(app, ["audit", "verify", "signed1"]).exit_code == 1

    monkeypatch.delenv("SEAWALL_AUDIT_KEY")
    missing = runner.invoke(app, ["audit", "verify", "signed1"])
    assert missing.exit_code == 1 and "key is needed" in missing.output
