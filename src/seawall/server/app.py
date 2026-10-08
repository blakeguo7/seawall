"""The HTTP application: routes, authentication and error handling.

Built on Starlette. Handlers are thin: they parse a request, call :class:`SessionManager`, and
shape the answer. Everything under ``/v1`` needs ``Authorization: Bearer <token>``; ``/healthz``
does not.
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from seawall.config import load_settings
from seawall.server.config import ConfigError, ServerConfig, is_loopback
from seawall.server.errors import ApiError
from seawall.server.manager import ClientFactory, RunHandle, SessionManager
from seawall.server.schemas import ApprovalIn, MessageIn, SessionSpec
from seawall.server.store import EventRow, SessionStore

log = logging.getLogger(__name__)

API = "/v1"
M = TypeVar("M", bound=BaseModel)


def _version() -> str:
    try:
        return metadata.version("seawall")
    except metadata.PackageNotFoundError:
        return "unknown"


# --- middleware -------------------------------------------------------------------------------


class HeadersMiddleware:
    """Marks every response as uncacheable and not to be content-sniffed.

    These responses are private (sessions, conversations, audit records) and must not be stored
    by a shared cache between the server and the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", "no-store")
                headers.setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AuthMiddleware:
    """Requires the bearer token on everything but ``/healthz``.

    Without a token (loopback development only) it instead refuses requests that look like they
    come from a web page: a ``Host`` that is not a loopback name (DNS rebinding) or any ``Origin``
    header (a cross-site request). Otherwise a page the user happened to open could drive the
    agent through their own machine.
    """

    def __init__(self, app: ASGIApp, *, token: str | None) -> None:
        self.app = app
        self._token = token.encode("utf-8") if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]}
        failure = self._check(headers)
        if failure is not None:
            response = JSONResponse(
                failure.to_body(),
                status_code=failure.status,
                headers={"WWW-Authenticate": "Bearer"} if failure.status == 401 else None,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, headers: dict[str, str]) -> ApiError | None:
        if self._token is None:
            host = headers.get("host", "")
            hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if not is_loopback(hostname.strip("[]")):
                return ApiError(403, "forbidden_host", "this server only answers on a loopback address")
            if "origin" in headers:
                return ApiError(403, "forbidden_origin", "browser requests are not accepted without a token")
            return None
        scheme, _, given = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode("utf-8"), self._token):
            return ApiError(401, "unauthorized", "send the server token as 'Authorization: Bearer <token>'")
        return None


# --- helpers ----------------------------------------------------------------------------------


def _manager(request: Request) -> SessionManager:
    manager: SessionManager = request.app.state.manager
    return manager


def _config(request: Request) -> ServerConfig:
    config: ServerConfig = request.app.state.config
    return config


async def _read_json(request: Request, *, required: bool = True) -> Any:
    """Parse a JSON body, refusing oversized or mistyped ones before reading them in full."""
    limit = _config(request).max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "body_too_large", f"request bodies are limited to {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "body_too_large", f"request bodies are limited to {limit} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        if required:
            raise ApiError(400, "empty_body", "a JSON body is required")
        return {}
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "send the body as application/json")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_json", f"the body is not valid JSON: {exc}") from exc


def _parse(model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}" for error in exc.errors()
        )
        raise ApiError(422, "invalid_request", problems) from exc


def _int_param(request: Request, name: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_parameter", f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ApiError(400, "bad_parameter", f"{name} must be between {low} and {high}")
    return value


def _sse(request: Request, events: AsyncIterator[EventRow]) -> EventSourceResponse:
    async def frames() -> AsyncIterator[dict[str, str]]:
        async for row in events:
            yield {
                "id": str(row.seq),
                "event": row.type,
                "data": json.dumps(
                    {"seq": row.seq, "ts": row.ts, "type": row.type, "session_id": row.session_id, "data": row.data},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(
        frames(),
        ping=_config(request).heartbeat_seconds,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _run_accepted(run: RunHandle) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "events_url": f"{API}/sessions/{run.session_id}/events?after={run.seq_before}&until_run={run.id}",
    }


# --- handlers ---------------------------------------------------------------------------------


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": _version()})


async def server_status(request: Request) -> Response:
    return JSONResponse(_manager(request).status())


async def create_session(request: Request) -> Response:
    spec = _parse(SessionSpec, await _read_json(request, required=False))
    row, warnings = await _manager(request).create_session(spec)
    body = row.to_dict()
    if warnings:
        body["warnings"] = warnings
    return JSONResponse(body, status_code=201, headers={"Location": f"{API}/sessions/{row.id}"})


async def list_sessions(request: Request) -> Response:
    manager = _manager(request)
    limit = _int_param(request, "limit", 50, low=1, high=200)
    offset = _int_param(request, "offset", 0, low=0, high=10**9)
    rows = await manager.store.list_sessions(limit=limit, offset=offset)
    return JSONResponse(
        {"sessions": [row.to_dict() for row in rows], "total": await manager.store.count_sessions()}
    )


async def get_session(request: Request) -> Response:
    row = await _manager(request).get_session(request.path_params["session_id"])
    return JSONResponse(row.to_dict())


async def delete_session(request: Request) -> Response:
    await _manager(request).delete_session(request.path_params["session_id"])
    return Response(status_code=204)


async def post_message(request: Request) -> Response:
    """Send a message. Three ways to get the answer, by how the request is made:

    * default: ``202`` with the run id and the URL of its event stream;
    * ``?wait=1``: hold the request until the run ends and return its result as JSON;
    * ``Accept: text/event-stream``: stream the run's events on this response.

    The run does not depend on the connection: if the client goes away it carries on, and its
    events can be read later from the stream endpoint.
    """
    manager = _manager(request)
    session_id = request.path_params["session_id"]
    message = _parse(MessageIn, await _read_json(request))
    run = await manager.submit(session_id, message.text)
    if "text/event-stream" in request.headers.get("accept", ""):
        return _sse(request, manager.stream_events(session_id, run.seq_before, until_run=run.id))
    if request.query_params.get("wait", "").lower() in {"1", "true", "yes"}:
        await run.done.wait()
        return JSONResponse(run.result)
    accepted = _run_accepted(run)
    return JSONResponse(accepted, status_code=202, headers={"Location": accepted["events_url"]})


async def get_messages(request: Request) -> Response:
    messages = await _manager(request).messages(request.path_params["session_id"])
    return JSONResponse({"messages": messages})


async def interrupt(request: Request) -> Response:
    interrupted = await _manager(request).interrupt(request.path_params["session_id"])
    return JSONResponse({"interrupted": interrupted})


async def stream_events(request: Request) -> Response:
    manager = _manager(request)
    session_id = request.path_params["session_id"]
    await manager.get_session(session_id)  # a 404 before any stream starts
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        after = int(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_parameter", "the event id must be an integer") from exc
    if after < 0:
        raise ApiError(400, "bad_parameter", "the event id cannot be negative")
    return _sse(request, manager.stream_events(session_id, after, until_run=request.query_params.get("until_run")))


async def list_approvals(request: Request) -> Response:
    manager = _manager(request)
    session_id = request.path_params["session_id"]
    await manager.get_session(session_id)
    status = request.query_params.get("status")
    return JSONResponse({"approvals": await manager.store.list_approvals(session_id, status=status)})


async def resolve_approval(request: Request) -> Response:
    answer = _parse(ApprovalIn, await _read_json(request))
    result = await _manager(request).resolve_approval(
        request.path_params["session_id"],
        request.path_params["approval_id"],
        approved=answer.approved,
        note=answer.note,
    )
    return JSONResponse(result)


async def list_runs(request: Request) -> Response:
    manager = _manager(request)
    session_id = request.path_params["session_id"]
    await manager.get_session(session_id)
    limit = _int_param(request, "limit", 20, low=1, high=200)
    return JSONResponse({"runs": await manager.store.list_runs(session_id, limit=limit)})


async def get_audit(request: Request) -> Response:
    params = request.query_params
    result = await _manager(request).audit(
        request.path_params["session_id"],
        tool=params.get("tool"),
        decision=params.get("decision"),
        min_risk=params.get("min_risk"),
        limit=_int_param(request, "limit", 500, low=1, high=5000),
    )
    return JSONResponse(result)


# --- errors -----------------------------------------------------------------------------------


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse(exc.to_body(), status_code=exc.status, headers=headers)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return JSONResponse({"error": {"code": code, "message": str(exc.detail)}}, status_code=exc.status_code, headers=exc.headers)


async def _unexpected(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"error": {"code": "internal", "message": "internal error"}}, status_code=500)


# --- assembly ---------------------------------------------------------------------------------


def check_environment() -> None:
    """Refuse to serve in an environment where sessions would interfere with each other."""
    if load_settings().sandbox.enabled:
        raise ConfigError(
            "the Docker sandbox is a single per-process session and cannot serve concurrent sessions; "
            "disable it (sandbox.enabled) to run the server"
        )


def create_app(config: ServerConfig, *, client_factory: ClientFactory | None = None) -> Starlette:
    """Build the application. The database is opened and the runtime started when it starts serving.

    ``client_factory`` replaces the model client (tests use a scripted one).
    """
    config.validate()
    store = SessionStore(config.db_path)
    manager = SessionManager(config, store, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        check_environment()
        await store.open()
        await manager.recover()
        try:
            yield
        finally:
            await manager.shutdown()
            await store.close()

    sessions = f"{API}/sessions/{{session_id}}"
    routes = [
        Route("/healthz", health),
        Route(f"{API}/status", server_status),
        Route(f"{API}/sessions", create_session, methods=["POST"]),
        Route(f"{API}/sessions", list_sessions, methods=["GET"]),
        Route(sessions, get_session, methods=["GET"]),
        Route(sessions, delete_session, methods=["DELETE"]),
        Route(f"{sessions}/messages", post_message, methods=["POST"]),
        Route(f"{sessions}/messages", get_messages, methods=["GET"]),
        Route(f"{sessions}/interrupt", interrupt, methods=["POST"]),
        Route(f"{sessions}/events", stream_events, methods=["GET"]),
        Route(f"{sessions}/approvals", list_approvals, methods=["GET"]),
        Route(f"{sessions}/approvals/{{approval_id}}", resolve_approval, methods=["POST"]),
        Route(f"{sessions}/runs", list_runs, methods=["GET"]),
        Route(f"{sessions}/audit", get_audit, methods=["GET"]),
    ]
    exception_handlers: dict[Any, Callable[[Request, Exception], Awaitable[Response]]] = {
        ApiError: _api_error,
        HTTPException: _http_error,
        Exception: _unexpected,
    }
    app = Starlette(
        routes=routes,
        middleware=[Middleware(HeadersMiddleware), Middleware(AuthMiddleware, token=config.token)],
        exception_handlers=exception_handlers,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.manager = manager
    return app
