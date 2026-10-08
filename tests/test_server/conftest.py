"""Fixtures for the HTTP service tests.

``client`` drives the app in-process (fast; finite responses only). ``live`` runs it on a real
socket with uvicorn, which is what streaming, resuming and shutdown need.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus
from starlette.testclient import TestClient

from seawall.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from seawall.api.usage import UsageSnapshot
from seawall.server.app import create_app
from seawall.server.config import ServerConfig
from seawall.server.serve import Server
from seawall.server.store import SessionRow
from tests.fakes import ScriptedClient, text_message

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Provider:
    """Stands in for the model: hands each session a client, by id or a default."""

    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}
        self.default: Callable[[], Any] = lambda: ScriptedClient(text_message("hello from the model"))
        self.built: list[str] = []

    def script(self, session_id: str, client: Any) -> None:
        self.clients[session_id] = client

    def __call__(self, row: SessionRow) -> Any:
        self.built.append(row.id)
        return self.clients.get(row.id) or self.default()


class GatedClient:
    """A model that answers only once the test says so. Cancelled calls are recorded.

    ``started`` is set when the first request arrives, so a test can wait until the run really is
    mid-call before it interrupts it or looks at the server's state.
    """

    def __init__(self, text: str = "finally done", *, open: bool = False) -> None:
        self.gate = threading.Event()
        if open:
            self.gate.set()
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.text = text
        self.requests: list[Any] = []

    def release(self) -> None:
        self.gate.set()

    async def stream_message(self, request):
        self.requests.append(request)
        self.started.set()
        try:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield ApiTextDeltaEvent(text=self.text)
        yield ApiMessageCompleteEvent(
            message=text_message(self.text), usage=UsageSnapshot(input_tokens=10, output_tokens=5), stop_reason=None
        )


class StreamingClient:
    """A model that produces its answer in pieces, with a pause between them."""

    def __init__(self, pieces: list[str], pause: float = 0.15) -> None:
        self.pieces, self.pause = pieces, pause

    async def stream_message(self, request):
        for piece in self.pieces:
            await asyncio.sleep(self.pause)
            yield ApiTextDeltaEvent(text=piece)
        yield ApiMessageCompleteEvent(
            message=text_message("".join(self.pieces)),
            usage=UsageSnapshot(input_tokens=10, output_tokens=5),
            stop_reason=None,
        )


class BrokenClient:
    """A model whose every call raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("the model exploded")

    async def stream_message(self, request):
        raise self.error
        yield  # pragma: no cover  (makes this an async generator)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path: Path, workspace: Path) -> ServerConfig:
    return ServerConfig(
        workspace_root=workspace,
        db_path=tmp_path / "server" / "sessions.db",
        token=TOKEN,
        port=0,
        poll_seconds=0.05,
        heartbeat_seconds=30,
        lease_seconds=5,
        approval_timeout_seconds=5,
        run_timeout_seconds=30,
    )


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def make_client(config: ServerConfig, provider: Provider):
    """``with make_client(max_concurrent_runs=1) as client:`` for a server with tweaked settings."""

    @contextmanager
    def make(**overrides: Any) -> Iterator[TestClient]:
        app = create_app(replace(config, **overrides), client_factory=provider)
        with TestClient(app, headers=AUTH, raise_server_exceptions=False) as client:
            yield client

    return make


@pytest.fixture
def client(make_client) -> Iterator[TestClient]:
    with make_client() as c:
        yield c


class LiveServer:
    """The app on a real socket, in a thread."""

    def __init__(self, app: Any) -> None:
        self.server = Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=3),
            app.state.manager,
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the test server did not start")
            time.sleep(0.01)

    @property
    def url(self) -> str:
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        """Shut down the way a SIGTERM would."""
        self.server.stop()
        self.thread.join(15)

    @property
    def stopped(self) -> bool:
        return not self.thread.is_alive()

    def http(self, **kwargs: Any) -> httpx.Client:
        # verify=False: the server speaks plain http, and building a CA bundle for every client
        # costs more than the requests do. trust_env=False: on a Mac with a system proxy, httpx
        # would send even loopback requests through it, and the proxy answers 502.
        return httpx.Client(base_url=self.url, headers=AUTH, timeout=15, verify=False, trust_env=False, **kwargs)


@pytest.fixture
def make_live(config: ServerConfig, provider: Provider):
    started: list[LiveServer] = []

    def make(**overrides: Any) -> LiveServer:
        # sse-starlette keeps one process-wide "the server is exiting" flag, set by any uvicorn
        # shutdown. Real servers exit afterwards; here several servers share one process.
        AppStatus.should_exit = False
        server = LiveServer(create_app(replace(config, **overrides), client_factory=provider))
        server.start()
        started.append(server)
        return server

    yield make
    for server in started:
        server.stop()
    AppStatus.should_exit = False


@pytest.fixture
def live(make_live) -> LiveServer:
    return make_live()


# --- helpers used across the tests --------------------------------------------------------------


def wait_for(condition: Callable[[], Any], *, timeout: float = 10.0, what: str = "condition") -> Any:
    """Poll until ``condition()`` is truthy and return its value."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Events of an SSE body as ``{"id", "event", "data"}`` dicts (data decoded); comments skipped."""
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            fields[name] = value[1:] if value.startswith(" ") else value
        if "data" in fields:
            events.append({"id": fields.get("id"), "event": fields.get("event"), "data": json.loads(fields["data"])})
    return events


def create_session(client: Any, **spec: Any) -> dict[str, Any]:
    response = client.post("/v1/sessions", json=spec)
    assert response.status_code == 201, response.text
    return response.json()


def run_message(client: Any, session_id: str, text: str = "hi") -> dict[str, Any]:
    """Send a message and wait for the run to end; returns the result JSON."""
    response = client.post(f"/v1/sessions/{session_id}/messages?wait=1", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def plain_help(*command: str) -> str:
    """The text of ``seawall <command> --help`` without colours or escape codes.

    On GitHub Actions Typer renders help as if for a terminal (it checks GITHUB_ACTIONS), so an
    option name can arrive split by bold and colour codes. Strip them, and make the page wide so
    nothing wraps.
    """
    from typer.testing import CliRunner

    from seawall.cli import app

    result = CliRunner().invoke(app, [*command, "--help"], env={"NO_COLOR": "1", "COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    return re.sub(r"\x1b\[[0-9;]*m", "", result.output)
