"""Tamper-evident audit log: one append-only JSON Lines file per session.

Every record carries the hash of the record before it, so editing, deleting,
reordering or inserting a record breaks the chain at a known line. ``verify_log``
finds the break.

What the chain does and does not give you:

* With the default plain SHA-256 chain, anyone who can write the file can also
  rewrite the whole chain. It detects accidental damage and sloppy edits, and
  truncation when the head hash was recorded somewhere else (``expected_head``).
* With a key (``HMAC-SHA-256``), a forger needs the key as well. Keep the key out
  of reach of the agent: it must not be readable by the process being audited,
  or the guarantee is gone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
GENESIS = "0" * 64
_BODY_KEYS = ("v", "seq", "ts", "session", "type", "data", "prev")


class AuditWriteError(RuntimeError):
    """The audit log could not be written and the log is configured to fail closed."""


class AuditSink(Protocol):
    """Where the engine sends audit events."""

    def record(self, event_type: str, **data: Any) -> None: ...

    def close(self) -> None: ...


class NullAudit:
    """Audit sink that drops everything; used when auditing is switched off."""

    def record(self, event_type: str, **data: Any) -> None:
        return None

    def close(self) -> None:
        return None


NULL_AUDIT = NullAudit()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default
    ).encode("utf-8")


def _digest(body: dict[str, Any], key: bytes | None) -> str:
    payload = _canonical(body)
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog:
    """Append-only, hash-chained event log for one session."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        key: bytes | None = None,
        fail_closed: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self._key = key or None
        self._fail_closed = fail_closed
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        self._warned = False
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._resume()
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._terminate_torn_line()

    @property
    def head(self) -> str:
        """Hash of the last record written; record it elsewhere to detect truncation."""
        return self._prev

    @property
    def count(self) -> int:
        return self._seq

    def record(self, event_type: str, **data: Any) -> None:
        """Append one event. Never raises unless the log fails closed."""
        with self._lock:
            if self._closed:
                log.debug("audit record %s dropped: log is closed", event_type)
                return
            if self._seq == 0:
                data = {
                    **data,
                    "chain": "hmac-sha256" if self._key else "sha256",
                    "format": FORMAT_VERSION,
                }
            body = {
                "v": FORMAT_VERSION,
                "seq": self._seq,
                "ts": self._clock().isoformat(timespec="milliseconds"),
                "session": self.session_id,
                "type": event_type,
                "data": data,
                "prev": self._prev,
            }
            try:
                digest = _digest(body, self._key)
                line = _canonical({**body, "hash": digest}) + b"\n"
                _write_all(self._fd, line)
            except (OSError, TypeError, ValueError) as exc:
                if self._fail_closed:
                    raise AuditWriteError(f"cannot write audit log {self.path}: {exc}") from exc
                if not self._warned:
                    self._warned = True
                    log.warning("audit log %s is not being written: %s", self.path, exc)
                return
            self._seq += 1
            self._prev = digest

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.fsync(self._fd)
            except OSError:
                pass
            os.close(self._fd)

    def _resume(self) -> None:
        """Continue the chain of an existing file; a damaged tail starts a new chain."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size == 0:
            return
        last = _last_line(self.path)
        try:
            record = json.loads(last)
            self._seq = int(record["seq"]) + 1
            self._prev = str(record["hash"])
        except (ValueError, KeyError, TypeError):
            log.warning("audit log %s has an unreadable last line; starting a new chain", self.path)

    def _terminate_torn_line(self) -> None:
        """A crash can leave a half-written line; never glue the next record onto it."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    _write_all(self._fd, b"\n")
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _last_line(path: Path) -> bytes:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        block = min(end, 65536)
        handle.seek(end - block)
        chunk = handle.read(block).rstrip(b"\n")
    return chunk.rsplit(b"\n", 1)[-1]


# ---------------------------------------------------------------------------
# Reading and verifying
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of checking one log file."""

    ok: bool
    records: int = 0
    session_id: str | None = None
    head: str | None = None
    closed: bool = False  # the last record is ``session.end``
    signed: bool = False  # the chain uses HMAC
    error: str | None = None
    error_line: int | None = None


def verify_log(
    path: Path, *, key: bytes | None = None, expected_head: str | None = None
) -> VerifyResult:
    """Check the hash chain of a log file.

    ``key`` is required for a signed log, and when given, an unsigned log is
    rejected (otherwise a forger could simply claim the log was never signed).
    ``expected_head`` is the head hash recorded when the session ended; it catches
    records removed from the end of the file.
    """

    def failed(message: str, line: int | None, **state: Any) -> VerifyResult:
        return VerifyResult(ok=False, error=message, error_line=line, **state)

    prev = GENESIS
    expected_seq = 0
    session_id: str | None = None
    signed = False
    last_type = ""
    try:
        handle = open(path, "rb")
    except OSError as exc:
        return failed(f"cannot read log: {exc}", None)
    with handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            state = {"records": expected_seq, "session_id": session_id, "signed": signed}
            try:
                record = json.loads(line)
            except ValueError:
                return failed("line is not valid JSON", lineno, **state)
            if not isinstance(record, dict) or not {*_BODY_KEYS, "hash"} <= record.keys():
                return failed("record is missing required fields", lineno, **state)
            if record["seq"] != expected_seq:
                return failed(
                    f"expected record {expected_seq}, found {record['seq']} "
                    "(a record was removed, reordered or duplicated)",
                    lineno,
                    **state,
                )
            if record["prev"] != prev:
                return failed(
                    "link to the previous record is broken "
                    "(an earlier record was changed, removed or inserted)",
                    lineno,
                    **state,
                )
            if session_id is None:
                session_id = record["session"]
                data = record["data"] if isinstance(record["data"], dict) else {}
                signed = data.get("chain") == "hmac-sha256"
                if signed and key is None:
                    return failed("the log is HMAC-signed; a key is needed to verify it", lineno, **state)
                if key is not None and not signed:
                    return failed("a key was given but the log is not signed", lineno, **state)
            elif record["session"] != session_id:
                return failed("record belongs to a different session", lineno, **state)
            body = {name: record[name] for name in _BODY_KEYS}
            expected = _digest(body, key if signed else None)
            if not isinstance(record["hash"], str) or not hmac.compare_digest(expected, record["hash"]):
                return failed("hash does not match the record (it was modified)", lineno, **state)
            prev = record["hash"]
            expected_seq += 1
            last_type = str(record["type"])
    state = {"records": expected_seq, "session_id": session_id, "signed": signed}
    if expected_seq == 0:
        return failed("log is empty", None, **state)
    if expected_head is not None and not hmac.compare_digest(expected_head, prev):
        return failed(
            "head hash differs from the recorded value "
            "(records were removed from the end, or the log was replaced)",
            None,
            head=prev,
            closed=last_type == "session.end",
            **state,
        )
    return VerifyResult(
        ok=True,
        records=expected_seq,
        session_id=session_id,
        head=prev,
        closed=last_type == "session.end",
        signed=signed,
    )


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of a log file; lines that are not JSON objects are skipped."""
    with open(path, "rb") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def key_from_env(name: str) -> bytes | None:
    """Read an HMAC key from the environment variable ``name``."""
    value = os.environ.get(name, "") if name else ""
    return value.encode("utf-8") if value else None
