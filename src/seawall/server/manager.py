"""Sessions in memory: runs, limits, leases, approvals and shutdown.

The database holds every session; this module holds the ones in use. A session becomes *live* when
a message arrives: its runtime (engine, tools, MCP connections, audit log) is built from the last
snapshot, the message runs, the snapshot is saved, and the runtime stays cached until the cache is
full and it is the least recently used idle one.

Rules the code below keeps:

* One run per session at a time (a second message gets 409), at most ``max_concurrent_runs`` across
  sessions, and ``max_queued_runs`` more waiting; past that a message gets 429.
* A session is run by one server process at a time: a lease in the database says which. A process
  that dies stops renewing it and the session becomes available again.
* ``run.finished`` is the last event of every run and is sent after the session is idle again, so a
  client that sees it can send its next message straight away.
* Whatever ends a run (completion, a limit, an interrupt, a crash in the runtime), the snapshot is
  saved, the approvals still waiting are closed and the client gets a ``run.finished``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping
from uuid import uuid4

from seawall.api.client import SupportsStreamingMessages
from seawall.api.usage import UsageSnapshot
from seawall.audit import audit_dir, key_from_env, read_records, verify_log
from seawall.audit.report import filter_records
from seawall.config import load_settings
from seawall.engine.query import MaxTurnsExceeded
from seawall.engine.stream_events import AssistantTurnComplete, RunFinished
from seawall.permissions.risk import RiskLevel
from seawall.server.approvals import SessionApprover
from seawall.server.config import ServerConfig
from seawall.server.errors import ApiError, not_found
from seawall.server.events import (
    TERMINAL_EVENT,
    EventBus,
    EventPublisher,
    EventStoreError,
    to_wire,
)
from seawall.server.schemas import SessionSpec
from seawall.server.store import EventRow, SessionRow, SessionStore
from seawall.services.session_storage import persistable_tool_metadata
from seawall.tools import create_default_tool_registry
from seawall.ui.overrides import SessionOverrides
from seawall.ui.runtime import (
    RuntimeBundle,
    build_runtime,
    close_runtime,
    prepare_submission,
    start_runtime,
)

log = logging.getLogger(__name__)

ClientFactory = Callable[[SessionRow], SupportsStreamingMessages]
SESSION_ID = re.compile(r"^[0-9a-f]{12}$")
LIMIT_FIELDS = ("max_budget_usd", "max_total_tokens", "max_seconds", "max_turns")
SHUTDOWN_GRACE_SECONDS = 20.0


class ModelNotConfigured(RuntimeError):
    """The server has no credentials for the model its sessions need."""


def _in_use(live: _Live) -> bool:
    """Whether a run is using the session's runtime right now. A run still waiting for a slot is not."""
    return live.run is not None and live.run.active


def _consume(task: asyncio.Task[Any]) -> None:
    """Mark a background task's outcome as seen, so an error nobody awaited is not reported twice."""
    if not task.cancelled():
        task.exception()


async def _no_answer(question: str) -> str:
    """Nobody can answer the model's questions over this API; an empty answer says so."""
    return ""


@dataclass(eq=False)
class RunHandle:
    """One message being processed."""

    id: str
    session_id: str
    seq_before: int = 0  # the session's last event number before this run; its events come after
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None  # the ``run.finished`` payload, once there is one
    stop: tuple[str, str] | None = None  # (stop_reason, detail) when someone asked it to end
    finishing: bool = False
    waiting: bool = True  # accepted, but it has neither taken a slot nor begun to end: what ``queued`` counts
    entered: bool = False  # the task has started; before that it must not be cancelled, only flagged
    active: bool = False  # it holds a slot, so its runtime is in use and must not be evicted
    cancel_sent: bool = False


@dataclass
class _Outcome:
    """What a run turned out to be, collected while it runs."""

    stop_reason: str = "error"
    detail: str = ""
    turns: int = 0
    duration_seconds: float = 0.0
    final_text: str = ""
    finished: RunFinished | None = None
    usage_before: UsageSnapshot = field(default_factory=UsageSnapshot)
    cost_before: float = 0.0


@dataclass(eq=False)
class _Live:
    """A session known to this process: its runtime if built, and the run in progress."""

    session_id: str
    bundle: RuntimeBundle | None = None
    approver: SessionApprover | None = None
    run: RunHandle | None = None
    publisher: EventPublisher | None = None
    last_used: float = field(default_factory=time.monotonic)
    deleting: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # guards building and closing the runtime
    building: asyncio.Task[RuntimeBundle] | None = None
    reserved: bool = False  # a runtime is being built for it: it already counts against the cache size

    # The :class:`~seawall.server.approvals.RunEvents` interface, for the approver.
    @property
    def run_id(self) -> str | None:
        return self.run.id if self.run is not None else None

    async def emit(self, type_: str, data: Mapping[str, Any]) -> None:
        if self.publisher is not None:
            await self.publisher.emit(type_, {"run_id": self.run_id, **data})

    def emit_nowait(self, type_: str, data: Mapping[str, Any]) -> None:
        if self.publisher is not None:
            self.publisher.emit_nowait(type_, {"run_id": self.run_id, **data})


class SessionManager:
    """Creates sessions and runs their messages. All methods run on the server's event loop."""

    def __init__(
        self,
        config: ServerConfig,
        store: SessionStore,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        """``client_factory`` supplies the model client for a session; leave it out to use the
        server's own provider configuration. Tests pass one that talks to a fake."""
        self.config = config
        self.store = store
        self.bus = EventBus()
        self._client_factory = client_factory
        self._live: dict[str, _Live] = {}
        self._slots = asyncio.Semaphore(config.max_concurrent_runs)
        self._room_lock = asyncio.Lock()  # one runtime at a time makes room in the cache
        self._inflight = 0  # runs accepted and not yet finished: waiting for a slot, running, or being closed
        self._running = 0
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._known_tools = {tool.name.lower() for tool in create_default_tool_registry().list_tools()}

    # --- life cycle -----------------------------------------------------------------------------

    async def recover(self) -> None:
        """Close out what a previous process left half done, and tell the clients that followed it."""
        self._loop = asyncio.get_running_loop()
        for run in await self.store.reset_interrupted():
            await self.store.append_event(
                run["session_id"],
                TERMINAL_EVENT,
                {
                    "run_id": run["run_id"],
                    "stop_reason": "interrupted",
                    "detail": "the server stopped before this run finished",
                },
            )
            self.bus.notify(run["session_id"])

    def begin_shutdown(self) -> None:
        """Stop taking new work and wake the streams so they can end. Call it on the event loop's thread."""
        self._stopping = True
        self.bus.close()

    def request_shutdown(self) -> None:
        """:meth:`begin_shutdown` from anywhere, such as a signal handler."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._stopping = True
        else:
            loop.call_soon_threadsafe(self.begin_shutdown)

    async def shutdown(self) -> None:
        """Interrupt the runs, wait for them to wrap up, and close every runtime."""
        self.begin_shutdown()
        runs = [live.run for live in self._live.values() if live.run is not None]
        for run in runs:
            self._stop_run(run, "interrupted", "the server is shutting down")
        if runs:
            _, pending = await asyncio.wait(
                [asyncio.ensure_future(run.done.wait()) for run in runs], timeout=SHUTDOWN_GRACE_SECONDS
            )
            for task in pending:
                task.cancel()
        for live in list(self._live.values()):
            await self._unload(live, force=True)

    def _accepting(self) -> None:
        if self._stopping:
            raise ApiError(503, "shutting_down", "the server is shutting down", retry_after=5)

    def status(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "server_id": cfg.server_id,
            "running": self._running,
            "queued": sum(1 for live in self._live.values() if live.run is not None and live.run.waiting),
            "loaded_sessions": sum(1 for live in self._live.values() if live.bundle is not None or live.reserved),
            "subscribers": self.bus.subscriber_count(),
            "limits": {
                "max_concurrent_runs": cfg.max_concurrent_runs,
                "max_queued_runs": cfg.max_queued_runs,
                "max_loaded_sessions": cfg.max_loaded_sessions,
                "max_sessions": cfg.max_sessions,
                "max_message_chars": cfg.max_message_chars,
                "run_timeout_seconds": cfg.run_timeout_seconds,
                **{name: getattr(cfg, name) for name in LIMIT_FIELDS},
            },
            "full_auto_allowed": cfg.allow_full_auto,
        }

    # --- sessions -------------------------------------------------------------------------------

    @staticmethod
    def _check_id(session_id: str) -> None:
        if not SESSION_ID.match(session_id):
            raise not_found("session")

    async def create_session(self, spec: SessionSpec) -> tuple[SessionRow, list[str]]:
        """Validate a client's request against the server's bounds and store the session.

        Returns the row and any warnings (tool names the server does not know).
        """
        self._accepting()
        cfg = self.config
        if await self.store.count_sessions() >= cfg.max_sessions:
            raise ApiError(403, "session_limit", f"the server keeps at most {cfg.max_sessions} sessions; delete some")
        if (spec.permission_mode == "full_auto" or spec.allowed_tools) and not cfg.allow_full_auto:
            raise ApiError(
                403,
                "unattended_disabled",
                "this server does not allow sessions that run tools without approval "
                "(permission_mode full_auto, or a non-empty allowed_tools)",
            )
        cwd = self._resolve_cwd(spec.cwd)
        spec = spec.model_copy(update={"permission_mode": spec.permission_mode or "default"})
        spec = self._apply_caps(spec, strict=True)
        warnings = [
            f"{field_name} lists {name!r}, which is not a tool this server knows"
            for field_name, names in (("allowed_tools", spec.allowed_tools), ("disallowed_tools", spec.disallowed_tools))
            for name in names
            if name.lower() not in self._known_tools and not name.startswith("mcp__")
        ]
        now = time.time()
        row = SessionRow(
            id=uuid4().hex[:12],
            title=spec.title,
            cwd=str(cwd),
            state="idle",
            spec=spec.model_dump(),
            created_at=now,
            updated_at=now,
        )
        await self.store.create_session(row)
        return row, warnings

    def _resolve_cwd(self, requested: str | None) -> Path:
        root = self.config.workspace_root.resolve()
        try:
            resolved = (root / requested).resolve() if requested else root
        except (ValueError, OSError) as exc:
            raise ApiError(422, "bad_cwd", f"not a usable directory: {exc}") from exc
        if resolved != root and root not in resolved.parents:
            raise ApiError(422, "cwd_outside_workspace", "cwd must be inside the server's workspace root")
        if not resolved.is_dir():
            raise ApiError(422, "cwd_not_found", "cwd is not a directory")
        return resolved

    def _apply_caps(self, spec: SessionSpec, *, strict: bool) -> SessionSpec:
        """Fit a session's limits under the server's. Too high is an error when ``strict``, else clamped.

        The server's caps become the session's limits when the client asked for none, so an
        operator who sets a budget gets it on every session.
        """
        updates: dict[str, Any] = {}
        for name in LIMIT_FIELDS:
            cap, wanted = getattr(self.config, name), getattr(spec, name)
            if cap is None:
                continue
            if wanted is None:
                updates[name] = cap
            elif wanted > cap:
                if strict:
                    raise ApiError(422, "limit_too_high", f"{name} {wanted:g} is above this server's limit {cap:g}")
                updates[name] = cap
        return spec.model_copy(update=updates) if updates else spec

    async def get_session(self, session_id: str) -> SessionRow:
        self._check_id(session_id)
        row = await self.store.get_session(session_id)
        if row is None:
            raise not_found("session")
        return row

    async def delete_session(self, session_id: str) -> None:
        row = await self.get_session(session_id)
        existing = self._live.get(session_id)
        if existing is not None and (existing.run is not None or existing.deleting):
            raise ApiError(409, "busy", "the session has a run in progress; interrupt it first")
        live = self._live.setdefault(session_id, _Live(session_id))
        live.deleting = True
        deleted = False
        try:
            if not await self.store.acquire_lease(session_id, self.config.server_id, self.config.lease_seconds):
                raise ApiError(409, "session_elsewhere", "the session is being run by another server")
            try:
                await self._unload(live, forget=False, force=True)
                await self.store.delete_session(row.id)
                deleted = True
            finally:
                await self.store.release_lease(session_id, self.config.server_id)
        finally:
            live.deleting = False
            if deleted:
                self._live.pop(session_id, None)
            else:
                self._forget_if_unused(live)  # a refused delete leaves the session exactly as it was

    # --- messages and runs ----------------------------------------------------------------------

    async def submit(self, session_id: str, text: str) -> RunHandle:
        """Accept a message and start processing it in the background.

        Raises 404 (no such session), 409 (a run is already in progress, or another server has
        the session), 413 (message too long), 429 (all slots and the queue are taken) or 503.
        """
        self._accepting()
        if len(text) > self.config.max_message_chars:
            raise ApiError(413, "message_too_long", f"messages are limited to {self.config.max_message_chars} characters")
        row = await self.get_session(session_id)
        current = self._live.get(session_id)
        if current is not None and current.run is not None and current.run.finishing:
            await current.run.done.wait()  # a moment: the run is writing its last events
        # From here to the run being registered nothing awaits, so two messages cannot both get in.
        live = self._live.setdefault(session_id, _Live(session_id))
        if live.deleting:
            raise not_found("session")
        if live.run is not None:
            raise ApiError(409, "busy", "the session is still processing a message; interrupt it or wait for run.finished")
        if self._inflight >= self.config.max_concurrent_runs + self.config.max_queued_runs:
            raise ApiError(429, "overloaded", "the server is busy; try again shortly", retry_after=2)
        run = RunHandle(id=uuid4().hex[:12], session_id=row.id)
        live.run = run
        self._inflight += 1
        recorded = False
        try:
            if not await self.store.acquire_lease(row.id, self.config.server_id, self.config.lease_seconds):
                raise ApiError(409, "session_elsewhere", "the session is being run by another server")
            run.seq_before = await self.store.last_seq(row.id)
            await self.store.create_run(run.id, row.id, text)
            recorded = True
            await self.store.update_session(row.id, state="queued")
        except BaseException:
            self._inflight -= 1
            live.run = None
            with contextlib.suppress(Exception):
                if recorded:  # the caller went away half way: leave no run that is "queued" for ever
                    await self.store.finish_run(run.id, stop_reason="error", detail="the message was not accepted", turns=0)
                    await self.store.update_session(row.id, state="idle")
                await self.store.release_lease(row.id, self.config.server_id)
            self._forget_if_unused(live)
            raise
        run.task = asyncio.get_running_loop().create_task(self._execute(live, run, text), name=f"run-{run.id}")
        return run

    def _stop_run(self, run: RunHandle, stop_reason: str, detail: str) -> bool:
        """Ask a run to end. Idempotent; False if it already ended."""
        if run.done.is_set():
            return False
        if run.stop is None:
            run.stop = (stop_reason, detail)
        if run.task is not None and run.entered and not run.cancel_sent:
            run.cancel_sent = True
            run.task.cancel()
        return True

    async def interrupt(self, session_id: str) -> bool:
        """Stop the session's current run. False if nothing was running."""
        await self.get_session(session_id)
        live = self._live.get(session_id)
        if live is None or live.run is None:
            return False
        return self._stop_run(live.run, "interrupted", "interrupted by request")

    async def _execute(self, live: _Live, run: RunHandle, text: str) -> None:
        run.entered = True
        cfg = self.config
        outcome = _Outcome()
        publisher = EventPublisher(self.store, self.bus, live.session_id, max_pending=cfg.max_pending_events)
        publisher.start()
        live.publisher = publisher
        keeper = asyncio.get_running_loop().create_task(self._keep_lease(run), name=f"lease-{run.id}")
        watchdog: asyncio.TimerHandle | None = None
        started = time.monotonic()
        try:
            try:
                if run.stop is not None:
                    raise asyncio.CancelledError  # stopped before it began
                async with self._slots:
                    self._running += 1
                    run.active = True
                    run.waiting = False
                    try:
                        if run.stop is not None:
                            raise asyncio.CancelledError  # stopped while waiting for a slot
                        watchdog = asyncio.get_running_loop().call_later(
                            cfg.run_timeout_seconds,
                            self._stop_run,
                            run,
                            "time_limit",
                            f"the run took longer than the server's limit of {cfg.run_timeout_seconds:g}s",
                        )
                        await self.store.mark_run_started(run.id)
                        await self.store.update_session(live.session_id, state="running")
                        await live.emit("run.started", {"text": text})
                        await self._drive(live, text, outcome)
                    finally:
                        run.active = False
                        self._running -= 1
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and hasattr(task, "uncancel"):
                    task.uncancel()
                outcome.stop_reason, outcome.detail = run.stop or ("interrupted", "interrupted")
            except (EventStoreError, ModelNotConfigured) as exc:
                outcome.stop_reason, outcome.detail = "error", str(exc)
            except Exception as exc:
                log.exception("run %s of session %s failed", run.id, live.session_id)
                outcome.stop_reason, outcome.detail = "error", f"{type(exc).__name__}: {exc}"
        finally:
            run.waiting = False  # however it ended, nothing is waiting for a slot any more
            if watchdog is not None:
                watchdog.cancel()
            keeper.cancel()
            outcome.duration_seconds = time.monotonic() - started
            await asyncio.shield(self._finish(live, run, outcome, publisher))

    async def _built(self, live: _Live) -> RuntimeBundle:
        """The session's runtime, building it if need be, even when the run is interrupted meanwhile.

        A build abandoned half way would leak what it had started (MCP server processes, the open
        audit log). So the build runs on its own task that an interrupt does not cancel: it
        finishes, the runtime is cached like any other, and it is closed with the rest.
        """
        if live.building is None or live.building.done():
            live.building = asyncio.get_running_loop().create_task(
                self._ensure_bundle(live), name=f"build-{live.session_id}"
            )
            live.building.add_done_callback(_consume)
        return await asyncio.shield(live.building)

    async def _drive(self, live: _Live, text: str, outcome: _Outcome) -> None:
        bundle = await self._built(live)
        engine = bundle.engine
        outcome.usage_before = engine.cost_tracker.total
        outcome.cost_before = engine.cost_tracker.cost_usd
        await asyncio.to_thread(prepare_submission, bundle, text)  # file and git lookups: not on the event loop
        try:
            async with contextlib.aclosing(engine.submit_message(text)) as stream:
                async for event in stream:
                    if isinstance(event, RunFinished):
                        outcome.finished = event
                        continue
                    if isinstance(event, AssistantTurnComplete) and event.message.text.strip():
                        outcome.final_text = event.message.text
                    wire = to_wire(event)
                    if wire is not None:
                        await live.emit(*wire)
        except MaxTurnsExceeded:
            pass  # the loop already reported it as a RunFinished with stop_reason max_turns
        if outcome.finished is not None:
            outcome.stop_reason = outcome.finished.stop_reason
            outcome.detail = outcome.finished.detail
            outcome.turns = outcome.finished.turns
        else:
            outcome.stop_reason, outcome.detail = "completed", ""

    async def _finish(self, live: _Live, run: RunHandle, outcome: _Outcome, publisher: EventPublisher) -> None:
        """Save the session, tell the clients, and make the session available again. Never raises."""
        run.finishing = True
        sid = live.session_id
        if live.building is not None:
            await asyncio.wait({live.building})  # an interrupted run still lets its runtime finish building
        usage_delta: dict[str, int] = {}
        cost_delta: float | None = None
        try:
            if live.approver is not None:
                live.approver.cancel_all("the run ended")
            bundle = live.bundle
            fields: dict[str, Any] = {"state": "idle", "last_stop_reason": outcome.stop_reason}
            if bundle is not None:
                tracker = bundle.engine.cost_tracker
                total = tracker.total
                before = outcome.usage_before
                usage_delta = {
                    "input_tokens": total.input_tokens - before.input_tokens,
                    "output_tokens": total.output_tokens - before.output_tokens,
                    "cache_read_input_tokens": total.cache_read_input_tokens - before.cache_read_input_tokens,
                    "cache_creation_input_tokens": total.cache_creation_input_tokens
                    - before.cache_creation_input_tokens,
                }
                cost_delta = None if tracker.unpriced_models else round(tracker.cost_usd - outcome.cost_before, 6)
                fields.update(
                    input_tokens=total.input_tokens,
                    output_tokens=total.output_tokens,
                    cache_read_tokens=total.cache_read_input_tokens,
                    cache_write_tokens=total.cache_creation_input_tokens,
                    cost_usd=tracker.cost_usd,
                    unpriced_models=list(tracker.unpriced_models),
                )
                await self.store.save_snapshot(
                    sid,
                    [message.model_dump(mode="json") for message in bundle.engine.messages],
                    persistable_tool_metadata(bundle.engine.tool_metadata),
                )
            await self.store.update_session(sid, **fields)
            await self.store.finish_run(
                run.id, stop_reason=outcome.stop_reason, detail=outcome.detail, turns=outcome.turns
            )
        except Exception:
            log.exception("could not save session %s after run %s", sid, run.id)
        result = {
            "run_id": run.id,
            "session_id": sid,
            "stop_reason": outcome.stop_reason,
            "detail": outcome.detail,
            "turns": outcome.turns,
            "duration_seconds": round(outcome.duration_seconds, 3),
            "usage": usage_delta,
            "cost_usd": cost_delta,
            "text": outcome.final_text,
        }
        try:
            await live.emit(TERMINAL_EVENT, {k: v for k, v in result.items() if k not in ("run_id", "session_id", "text")})
        except Exception:
            log.exception("could not send run.finished for run %s", run.id)
        try:
            await publisher.close()
        except Exception:
            log.exception("could not flush the events of run %s", run.id)
        with contextlib.suppress(Exception):
            await self.store.release_lease(sid, self.config.server_id)
        run.result = result
        live.run = None
        live.publisher = None
        live.last_used = time.monotonic()
        self._inflight -= 1
        run.done.set()
        self._forget_if_unused(live)

    async def _keep_lease(self, run: RunHandle) -> None:
        """Renew the session's lease while the run is queued or running."""
        interval = max(self.config.lease_seconds / 3, 0.05)
        try:
            while True:
                await asyncio.sleep(interval)
                if not await self.store.acquire_lease(run.session_id, self.config.server_id, self.config.lease_seconds):
                    self._stop_run(run, "error", "this server lost the session's lease to another server")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not renew the lease of session %s", run.session_id)

    # --- runtimes -------------------------------------------------------------------------------

    async def _ensure_bundle(self, live: _Live) -> RuntimeBundle:
        async with live.lock:
            if live.bundle is not None:
                return live.bundle
            row = await self.store.get_session(live.session_id)
            if row is None:
                raise RuntimeError("the session was deleted")
            async with self._room_lock:
                await self._make_room(keep=live)
                live.reserved = True
            try:
                spec = self._apply_caps(SessionSpec(**row.spec), strict=False)
                snapshot = await self.store.load_snapshot(row.id)
                approver = SessionApprover(row.id, self.store, live, timeout=self.config.approval_timeout_seconds)
                try:
                    bundle = await build_runtime(
                        cwd=row.cwd,
                        model=spec.model,
                        max_turns=spec.max_turns,
                        api_client=self._client_factory(row) if self._client_factory else None,
                        approver=approver,
                        ask_user_prompt=_no_answer,
                        restore_messages=(snapshot[0] or None) if snapshot is not None else None,
                        restore_tool_metadata=(snapshot[1] or None) if snapshot is not None else None,
                        enforce_max_turns=True,
                        permission_mode=spec.permission_mode or "default",
                        overrides=SessionOverrides.from_cli(
                            allowed_tools=spec.allowed_tools,
                            disallowed_tools=spec.disallowed_tools,
                            append_system_prompt=spec.append_system_prompt,
                            max_budget_usd=spec.max_budget_usd,
                            max_total_tokens=spec.max_total_tokens,
                            max_seconds=spec.max_seconds,
                        ),
                        session_id=row.id,
                    )
                except SystemExit as exc:
                    # build_runtime exits the process when no API key is configured. Raised inside a
                    # task, SystemExit would stop the event loop, so it becomes an ordinary error here.
                    raise ModelNotConfigured("the server has no usable model credentials (see its log)") from exc
                try:
                    await start_runtime(bundle)
                    bundle.audit.record(
                        "server.session_loaded",
                        server_id=self.config.server_id,
                        resumed=snapshot is not None,
                        spec={k: v for k, v in spec.model_dump().items() if k != "append_system_prompt"},
                    )
                except BaseException:
                    await close_runtime(bundle)
                    raise
                bundle.engine.cost_tracker.restore(
                    UsageSnapshot(
                        input_tokens=row.input_tokens,
                        output_tokens=row.output_tokens,
                        cache_read_input_tokens=row.cache_read_tokens,
                        cache_creation_input_tokens=row.cache_write_tokens,
                    ),
                    row.cost_usd,
                    row.unpriced_models or (),
                )
                live.bundle, live.approver = bundle, approver
                return bundle
            finally:
                live.reserved = False  # built, and counted as a bundle, or failed and gives the place back

    async def _make_room(self, *, keep: _Live) -> None:
        """Close idle runtimes, least recently used first, so that one more fits."""
        loaded = [live for live in self._live.values() if live.bundle is not None or live.reserved]
        excess = len(loaded) - (self.config.max_loaded_sessions - 1)
        if excess <= 0:
            return
        idle = sorted(
            (live for live in loaded if not _in_use(live) and live is not keep and not live.reserved),
            key=lambda live: live.last_used,
        )
        for live in idle[:excess]:
            await self._unload(live)

    async def _unload(self, live: _Live, *, forget: bool = True, force: bool = False) -> None:
        """Close a session's runtime. The conversation is already saved after every run.

        A session that has started running in the meantime is left alone unless ``force``.
        """
        async with live.lock:
            if _in_use(live) and not force:
                return
            bundle, live.bundle, live.approver = live.bundle, None, None
            if bundle is not None:
                try:
                    await close_runtime(bundle)
                except Exception:
                    log.exception("error while closing the runtime of session %s", live.session_id)
        if forget:
            self._forget_if_unused(live)

    def _forget_if_unused(self, live: _Live) -> None:
        if live.run is None and live.bundle is None and not live.deleting:
            if self._live.get(live.session_id) is live:
                del self._live[live.session_id]

    # --- reading --------------------------------------------------------------------------------

    async def stream_events(
        self, session_id: str, after: int = 0, *, until_run: str | None = None
    ) -> AsyncIterator[EventRow]:
        """Events of a session after number ``after``, then new ones as they are written.

        Ends when ``until_run`` finishes, when the session is deleted, or on shutdown. Everything
        comes from the database, so this behaves the same live, resumed, or for a run that another
        server process is executing (the stream then wakes by polling).
        """
        self._check_id(session_id)
        waiter = self.bus.subscribe(session_id)
        try:
            cursor = after
            while True:
                waiter.clear()
                rows = await self.store.events_after(session_id, cursor, limit=200)
                for row in rows:
                    cursor = row.seq
                    yield row
                    if until_run and row.type == TERMINAL_EVENT and row.data.get("run_id") == until_run:
                        return
                if rows:
                    continue
                if self.bus.closed or await self.store.get_session(session_id) is None:
                    return
                await waiter.wait(self.config.poll_seconds)
        finally:
            waiter.close()

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        """The conversation as of the end of the last finished run."""
        await self.get_session(session_id)
        snapshot = await self.store.load_snapshot(session_id)
        return snapshot[0] if snapshot is not None else []

    async def resolve_approval(
        self, session_id: str, approval_id: str, *, approved: bool, note: str = ""
    ) -> dict[str, Any]:
        await self.get_session(session_id)
        approval = await self.store.get_approval(session_id, approval_id)
        if approval is None:
            raise not_found("approval")
        live = self._live.get(session_id)
        if live is None or live.approver is None or not live.approver.resolve(approval_id, approved=approved, note=note):
            if approval["status"] != "pending":
                raise ApiError(409, "already_resolved", f"the approval is already {approval['status']}")
            raise ApiError(409, "not_pending_here", "the approval is not waiting in this server (the session may run elsewhere)")
        return {**approval, "status": "approved" if approved else "denied", "decided_by": "api", "note": note}

    async def audit(
        self,
        session_id: str,
        *,
        tool: str | None = None,
        decision: str | None = None,
        min_risk: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """The session's audit records (the newest ``limit``), with the result of checking its hash chain."""
        await self.get_session(session_id)
        try:
            level = RiskLevel.parse(min_risk) if min_risk else None
        except ValueError as exc:
            raise ApiError(422, "bad_min_risk", str(exc)) from exc
        audit_settings = load_settings().audit
        path = audit_dir(audit_settings) / f"{session_id}.jsonl"
        key = key_from_env(audit_settings.key_env)

        def read() -> dict[str, Any]:
            if not path.is_file():
                return {"verified": None, "error": "no audit log for this session", "total": 0, "records": []}
            check = verify_log(path, key=key)
            records = list(filter_records(read_records(path), tool=tool, decision=decision, min_risk=level))
            return {
                "verified": check.ok,
                "error": check.error,
                "total": check.records,
                "records": records[-limit:],
            }

        return {"session_id": session_id, **await asyncio.to_thread(read)}
