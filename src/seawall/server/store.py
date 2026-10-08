"""SQLite persistence for the service: sessions, events, snapshots, approvals, runs and leases.

One connection guarded by a lock, in WAL mode, with every call moved off the event loop. The
data is small and the calls are short, so this is simpler than a pool and plenty fast. Several
server processes can share one database file: each write that must not interleave
(event numbering, leases) runs in a ``BEGIN IMMEDIATE`` transaction, which takes SQLite's file
write lock.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT NOT NULL,
    state TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    unpriced_models TEXT NOT NULL DEFAULT '[]',
    last_stop_reason TEXT,
    lease_owner TEXT,
    lease_expires_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    tool_metadata_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT,
    tool TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk TEXT NOT NULL,
    risk_reasons_json TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS approvals_by_session ON approvals (session_id, status);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    stop_reason TEXT,
    detail TEXT,
    turns INTEGER,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS runs_by_session ON runs (session_id, created_at);
"""

@dataclass
class SessionRow:
    id: str
    title: str | None
    cwd: str
    state: str  # idle | queued | running
    spec: dict[str, Any]
    created_at: float
    updated_at: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_models: list[str] | None = None
    last_stop_reason: str | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "state": self.state,
            "spec": self.spec,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_input_tokens": self.cache_read_tokens,
                "cache_creation_input_tokens": self.cache_write_tokens,
            },
            "cost_usd": None if self.unpriced_models else round(self.cost_usd, 6),
            "unpriced_models": self.unpriced_models or [],
            "last_stop_reason": self.last_stop_reason,
        }


@dataclass(frozen=True)
class EventRow:
    session_id: str
    seq: int
    ts: float
    type: str
    data: dict[str, Any]


def _session_from(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id=row["id"],
        title=row["title"],
        cwd=row["cwd"],
        state=row["state"],
        spec=json.loads(row["spec_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        cost_usd=row["cost_usd"],
        unpriced_models=json.loads(row["unpriced_models"]) or None,
        last_stop_reason=row["last_stop_reason"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
    )


class SessionStore:
    """The service's database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # --- plumbing -------------------------------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} was written by a newer version (schema {version})")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._conn = conn

    async def close(self) -> None:
        def close() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None

        await asyncio.to_thread(close)

    async def _call(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("the store is not open")
                return fn(self._conn)

        return await asyncio.to_thread(run)

    @staticmethod
    def _transaction(conn: sqlite3.Connection, body: Callable[[], T]) -> T:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = body()
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    # --- sessions -------------------------------------------------------------------------------

    async def create_session(self, row: SessionRow) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, state, spec_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.title, row.cwd, row.state, json.dumps(row.spec), row.created_at, row.updated_at),
            )

        await self._call(run)

    async def get_session(self, session_id: str) -> SessionRow | None:
        def run(conn: sqlite3.Connection) -> SessionRow | None:
            found = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return _session_from(found) if found else None

        return await self._call(run)

    async def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[SessionRow]:
        def run(conn: sqlite3.Connection) -> list[SessionRow]:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            return [_session_from(r) for r in rows]

        return await self._call(run)

    async def count_sessions(self) -> int:
        return await self._call(lambda conn: conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    async def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {
            "title", "state", "updated_at", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "cost_usd", "unpriced_models", "last_stop_reason",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if "unpriced_models" in fields:
            fields["unpriced_models"] = json.dumps(fields["unpriced_models"])
        fields.setdefault("updated_at", time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id))

        await self._call(run)

    async def delete_session(self, session_id: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            def body() -> None:
                for table in ("events", "snapshots", "approvals", "runs"):
                    conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

            self._transaction(conn, body)

        await self._call(run)

    async def reset_interrupted(self) -> list[dict[str, Any]]:
        """After a crash or restart: end what was mid-flight and no live server owns.

        A session counts as owned while another server holds an unexpired lease on it; its runs
        and approvals are left alone. Returns the runs that were ended, so the caller can tell the
        clients that were following them.
        """

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            def body() -> list[dict[str, Any]]:
                now = time.time()
                orphaned = "(SELECT id FROM sessions WHERE lease_owner IS NULL OR lease_expires_at < ?)"
                runs = conn.execute(
                    "SELECT id, session_id FROM runs WHERE status IN ('queued', 'running') "
                    f"AND session_id IN {orphaned}",
                    (now,),
                ).fetchall()
                conn.execute(
                    "UPDATE approvals SET status = 'expired', resolved_at = ? WHERE status = 'pending' "
                    f"AND session_id IN {orphaned}",
                    (now, now),
                )
                conn.execute(
                    "UPDATE runs SET status = 'interrupted', stop_reason = 'interrupted', finished_at = ? "
                    f"WHERE status IN ('queued', 'running') AND session_id IN {orphaned}",
                    (now, now),
                )
                conn.execute(
                    "UPDATE sessions SET state = 'idle', updated_at = ?, lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE state IN ('queued', 'running') "
                    "AND (lease_owner IS NULL OR lease_expires_at < ?)",
                    (now, now),
                )
                return [{"run_id": r["id"], "session_id": r["session_id"]} for r in runs]

            return self._transaction(conn, body)

        return await self._call(run)

    # --- leases ---------------------------------------------------------------------------------

    async def acquire_lease(self, session_id: str, owner: str, seconds: float) -> bool:
        """Take (or renew) the right to run a session. False if another live owner holds it."""

        def run(conn: sqlite3.Connection) -> bool:
            def body() -> bool:
                now = time.time()
                cursor = conn.execute(
                    "UPDATE sessions SET lease_owner = ?, lease_expires_at = ? WHERE id = ? "
                    "AND (lease_owner IS NULL OR lease_owner = ? OR lease_expires_at < ?)",
                    (owner, now + seconds, session_id, owner, now),
                )
                return cursor.rowcount == 1

            return self._transaction(conn, body)

        return await self._call(run)

    async def release_lease(self, session_id: str, owner: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE sessions SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND lease_owner = ?",
                (session_id, owner),
            )

        await self._call(run)

    # --- events ---------------------------------------------------------------------------------

    async def append_event(self, session_id: str, type_: str, data: dict[str, Any]) -> tuple[int, float]:
        """Store an event under the next sequence number of its session and return (seq, ts)."""
        payload = json.dumps(data, ensure_ascii=False, default=str)

        def run(conn: sqlite3.Connection) -> tuple[int, float]:
            def body() -> tuple[int, float]:
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
                ts = time.time()
                conn.execute(
                    "INSERT INTO events (session_id, seq, ts, type, data_json) VALUES (?, ?, ?, ?, ?)",
                    (session_id, seq, ts, type_, payload),
                )
                return seq, ts

            return self._transaction(conn, body)

        return await self._call(run)

    async def events_after(self, session_id: str, after: int, *, limit: int = 500) -> list[EventRow]:
        def run(conn: sqlite3.Connection) -> list[EventRow]:
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (session_id, after, limit),
            ).fetchall()
            return [EventRow(r["session_id"], r["seq"], r["ts"], r["type"], json.loads(r["data_json"])) for r in rows]

        return await self._call(run)

    async def last_seq(self, session_id: str) -> int:
        return await self._call(
            lambda conn: conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )

    # --- snapshots ------------------------------------------------------------------------------

    async def save_snapshot(self, session_id: str, messages: list[dict[str, Any]], tool_metadata: dict[str, Any]) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO snapshots (session_id, messages_json, tool_metadata_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET messages_json = excluded.messages_json, "
                "tool_metadata_json = excluded.tool_metadata_json, updated_at = excluded.updated_at",
                (session_id, json.dumps(messages), json.dumps(tool_metadata), time.time()),
            )

        await self._call(run)

    async def load_snapshot(self, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        def run(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
            row = conn.execute("SELECT * FROM snapshots WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return json.loads(row["messages_json"]), json.loads(row["tool_metadata_json"])

        return await self._call(run)

    # --- approvals ------------------------------------------------------------------------------

    async def save_approval(self, approval: dict[str, Any]) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO approvals (id, session_id, run_id, tool, summary, reason, risk, risk_reasons_json, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    approval["id"], approval["session_id"], approval.get("run_id"), approval["tool_name"],
                    approval["summary"], approval["reason"], approval["risk"],
                    json.dumps(approval["risk_reasons"]), approval["created_at"],
                ),
            )

        await self._call(run)

    async def resolve_approval(self, approval_id: str, status: str, decided_by: str, note: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, note = ?, resolved_at = ? WHERE id = ?",
                (status, decided_by, note, time.time(), approval_id),
            )

        await self._call(run)

    async def list_approvals(self, session_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            query = "SELECT * FROM approvals WHERE session_id = ?"
            args: list[Any] = [session_id]
            if status:
                query += " AND status = ?"
                args.append(status)
            rows = conn.execute(query + " ORDER BY created_at", args).fetchall()
            return [
                {
                    "id": r["id"], "session_id": r["session_id"], "run_id": r["run_id"], "tool_name": r["tool"],
                    "summary": r["summary"], "reason": r["reason"], "risk": r["risk"],
                    "risk_reasons": json.loads(r["risk_reasons_json"]), "status": r["status"],
                    "decided_by": r["decided_by"], "note": r["note"], "created_at": r["created_at"],
                    "resolved_at": r["resolved_at"],
                }
                for r in rows
            ]

        return await self._call(run)

    async def get_approval(self, session_id: str, approval_id: str) -> dict[str, Any] | None:
        for approval in await self.list_approvals(session_id):
            if approval["id"] == approval_id:
                return approval
        return None

    # --- runs -----------------------------------------------------------------------------------

    async def create_run(self, run_id: str, session_id: str, prompt: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO runs (id, session_id, status, prompt, created_at) VALUES (?, ?, 'queued', ?, ?)",
                (run_id, session_id, prompt, time.time()),
            )

        await self._call(run)

    async def mark_run_started(self, run_id: str) -> None:
        await self._call(
            lambda conn: conn.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (time.time(), run_id))
        )

    async def finish_run(self, run_id: str, *, stop_reason: str, detail: str, turns: int) -> None:
        status = "finished" if stop_reason == "completed" else stop_reason
        await self._call(
            lambda conn: conn.execute(
                "UPDATE runs SET status = ?, stop_reason = ?, detail = ?, turns = ?, finished_at = ? WHERE id = ?",
                (status, stop_reason, detail, turns, time.time(), run_id),
            )
        )

    async def list_runs(self, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?", (session_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._call(run)
