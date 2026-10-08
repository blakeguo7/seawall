"""Tests for the tamper-evident audit log."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from seawall.audit import (
    NULL_AUDIT,
    AuditLog,
    AuditWriteError,
    open_session_audit,
    read_records,
    resolve_log,
    verify_log,
)
from seawall.audit.log import GENESIS
from seawall.config.settings import AuditSettings


def write_log(path: Path, count: int = 4, **kwargs) -> AuditLog:
    log = AuditLog(path, session_id="sess1", **kwargs)
    log.record("session.start", cwd="/work")
    for index in range(count - 2):
        log.record("tool.decision", call_id=f"c{index}", decision="allow")
    log.record("session.end")
    log.close()
    return log


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def rewrite(path: Path, new_lines: list[str]) -> None:
    path.write_text("\n".join(new_lines) + "\n")


# --- writing --------------------------------------------------------------------


def test_records_form_a_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "sess1.jsonl"
    log = write_log(path)
    records = list(read_records(path))

    assert [r["seq"] for r in records] == [0, 1, 2, 3]
    assert records[0]["prev"] == GENESIS
    for earlier, later in zip(records, records[1:]):
        assert later["prev"] == earlier["hash"]
    assert log.head == records[-1]["hash"]
    assert log.count == 4
    assert records[0]["data"]["chain"] == "sha256"
    assert records[0]["data"]["cwd"] == "/work"
    assert all(r["session"] == "sess1" for r in records)


def test_log_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "sess1.jsonl"
    write_log(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_records_are_one_json_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    for line in lines(path):
        assert isinstance(json.loads(line), dict)


def test_unserialisable_values_are_stringified(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="s")
    log.record("x", path=Path("/a/b"), items={1, 2})
    log.close()
    assert verify_log(path).ok


def test_concurrent_writers_keep_the_chain_intact(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="s")

    def worker(n: int) -> None:
        for i in range(50):
            log.record("tick", worker=n, i=i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.close()

    result = verify_log(path)
    assert result.ok and result.records == 300


def test_records_after_close_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = write_log(path)
    log.record("late")
    assert verify_log(path).records == 4


def test_resuming_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    first = AuditLog(path, session_id="sess1")
    first.record("a")
    first.close()
    second = AuditLog(path, session_id="sess1")
    second.record("b")
    second.close()

    result = verify_log(path)
    assert result.ok and result.records == 2
    assert [r["type"] for r in read_records(path)] == ["a", "b"]


def test_a_torn_last_line_is_not_glued_to_the_next_record(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="sess1")
    log.record("a")
    log.close()
    with open(path, "ab") as handle:
        handle.write(b'{"seq": 1, "half a rec')  # a crash in the middle of a write
    again = AuditLog(path, session_id="sess1")
    again.record("b")
    again.close()

    result = verify_log(path)
    assert result.ok is False
    assert result.error_line == 2  # the damage is reported where it is
    assert [r["type"] for r in read_records(path)] == ["a", "b"]  # and the new record survives


# --- verifying ----------------------------------------------------------------------


def test_a_good_log_verifies(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = write_log(path)
    result = verify_log(path, expected_head=log.head)
    assert result.ok is True
    assert result.records == 4
    assert result.session_id == "sess1"
    assert result.closed is True
    assert result.signed is False
    assert result.head == log.head


def test_a_log_without_session_end_is_not_closed(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="s")
    log.record("session.start")
    log.close()
    result = verify_log(path)
    assert result.ok is True and result.closed is False


def test_editing_a_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    content = lines(path)
    content[2] = content[2].replace('"allow"', '"deny"')
    rewrite(path, content)

    result = verify_log(path)
    assert result.ok is False
    assert result.error_line == 3
    assert "modified" in result.error


def test_deleting_a_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    content = lines(path)
    del content[1]
    rewrite(path, content)

    result = verify_log(path)
    assert result.ok is False
    assert result.error_line == 2
    assert "removed" in result.error


def test_inserting_a_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    content = lines(path)
    content.insert(2, content[1])
    rewrite(path, content)
    assert verify_log(path).ok is False


def test_reordering_records_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    content = lines(path)
    content[1], content[2] = content[2], content[1]
    rewrite(path, content)
    assert verify_log(path).ok is False


def test_garbage_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    with open(path, "a") as handle:
        handle.write("not json\n")
    result = verify_log(path)
    assert result.ok is False and "JSON" in result.error


def test_truncation_needs_the_recorded_head_to_be_noticed(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = write_log(path)
    rewrite(path, lines(path)[:2])  # an attacker drops the last records

    assert verify_log(path).ok is True  # a plain chain cannot see this on its own
    result = verify_log(path, expected_head=log.head)
    assert result.ok is False
    assert "head hash differs" in result.error


def test_mixing_sessions_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path)
    record = json.loads(lines(path)[1])
    record["session"] = "other"
    content = lines(path)
    content[1] = json.dumps(record)
    rewrite(path, content)
    assert verify_log(path).ok is False


def test_missing_and_empty_files(tmp_path: Path) -> None:
    assert verify_log(tmp_path / "nope.jsonl").ok is False
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert verify_log(empty).error == "log is empty"


# --- signed logs ----------------------------------------------------------------------


def test_signed_log_needs_its_key(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    write_log(path, key=b"secret")

    assert list(read_records(path))[0]["data"]["chain"] == "hmac-sha256"
    assert verify_log(path, key=b"secret").ok is True
    assert verify_log(path, key=b"secret").signed is True
    assert "key is needed" in verify_log(path).error
    assert "hash does not match" in verify_log(path, key=b"wrong").error


def test_a_forger_without_the_key_cannot_rewrite_a_signed_log(tmp_path: Path) -> None:
    signed = tmp_path / "signed.jsonl"
    write_log(signed, key=b"secret")
    # The forger rebuilds the whole chain, but as an unsigned one.
    forged = tmp_path / "forged.jsonl"
    log = AuditLog(forged, session_id="sess1")
    log.record("session.start", cwd="/work")
    log.record("session.end")
    log.close()

    assert verify_log(forged).ok is True
    result = verify_log(forged, key=b"secret")
    assert result.ok is False
    assert "not signed" in result.error


# --- failure handling -------------------------------------------------------------------


def test_write_failures_do_not_raise_by_default(tmp_path: Path, caplog) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="s")
    os.close(log._fd)  # break the file under the log
    log.record("a")
    log.record("b")  # no exception, one warning
    assert sum("is not being written" in r.message for r in caplog.records) == 1
    assert log.count == 0


def test_fail_closed_raises(tmp_path: Path) -> None:
    path = tmp_path / "sess1.jsonl"
    log = AuditLog(path, session_id="s", fail_closed=True)
    os.close(log._fd)
    with pytest.raises(AuditWriteError):
        log.record("a")


# --- settings and lookup ------------------------------------------------------------------


def test_open_session_audit_respects_the_enabled_switch(tmp_path: Path) -> None:
    assert open_session_audit(AuditSettings(enabled=False, directory=str(tmp_path)), "s1") is NULL_AUDIT
    assert list(tmp_path.iterdir()) == []


def test_open_session_audit_writes_into_the_configured_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEAWALL_AUDIT_KEY", "k")
    sink = open_session_audit(AuditSettings(directory=str(tmp_path / "logs")), "abc123")
    sink.record("session.start")
    sink.close()

    path = tmp_path / "logs" / "abc123.jsonl"
    assert verify_log(path, key=b"k").ok is True


def test_open_session_audit_defaults_to_the_data_dir() -> None:
    sink = open_session_audit(AuditSettings(), "s1")
    sink.record("session.start")
    sink.close()
    from seawall.config.paths import get_data_dir

    assert (get_data_dir() / "audit" / "s1.jsonl").is_file()


def test_unwritable_directory_degrades_or_fails_closed(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert open_session_audit(AuditSettings(directory=str(blocker / "sub")), "s") is NULL_AUDIT
    with pytest.raises(AuditWriteError):
        open_session_audit(AuditSettings(directory=str(blocker / "sub"), fail_closed=True), "s")


def test_resolve_log_by_prefix_and_path(tmp_path: Path) -> None:
    (tmp_path / "abc123.jsonl").write_text("")
    (tmp_path / "abd456.jsonl").write_text("")
    assert resolve_log(tmp_path, "abc") == tmp_path / "abc123.jsonl"
    assert resolve_log(tmp_path, "ab") is None  # ambiguous
    assert resolve_log(tmp_path, "zzz") is None
    assert resolve_log(tmp_path, str(tmp_path / "abd456.jsonl")) == tmp_path / "abd456.jsonl"
