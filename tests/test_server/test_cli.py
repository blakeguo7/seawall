"""``seawall serve``: argument checking, configuration, and the real process end to end."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from seawall.cli import app
from seawall.evals.grader import clean_env
from seawall.evals.runner import LOOPBACK_DIRECT
from seawall.evals.scripted import ScriptedServer, Turn
from seawall.server.config import TOKEN_ENV
from tests.test_server.conftest import TOKEN, plain_help, wait_for

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


def serve(*args: str, env: dict[str, str] | None = None):
    return runner.invoke(app, ["serve", *args], env=env)


def test_help_lists_the_options():
    text = plain_help("serve")
    for flag in ("--workspace", "--insecure-no-auth", "--allow-full-auto", "--max-concurrent-runs", "--token-env"):
        assert flag in text
    assert "--token " not in text  # a token on the command line would show up in `ps`


def test_the_workspace_is_required():
    assert serve().exit_code == 2


def test_it_refuses_to_start_without_a_token(workspace):
    result = serve("--workspace", str(workspace))
    assert result.exit_code == 2
    assert TOKEN_ENV in result.output


def test_a_short_token_is_refused(workspace):
    result = serve("--workspace", str(workspace), env={TOKEN_ENV: "short"})
    assert result.exit_code == 2 and "16 characters" in result.output


def test_a_missing_workspace_is_refused():
    result = serve("--workspace", "/definitely/not/here", env={TOKEN_ENV: TOKEN})
    assert result.exit_code == 2 and "not a directory" in result.output


def test_no_auth_is_only_for_loopback(workspace):
    result = serve("--workspace", str(workspace), "--insecure-no-auth", "--host", "0.0.0.0")
    assert result.exit_code == 2 and "loopback" in result.output


def test_the_docker_sandbox_is_refused_because_it_is_one_per_process(workspace, tmp_path):
    config_dir = Path(os.environ["SEAWALL_CONFIG_DIR"])
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text('{"sandbox": {"enabled": true}}')
    result = serve("--workspace", str(workspace), env={TOKEN_ENV: TOKEN})
    assert result.exit_code == 2 and "sandbox" in result.output


def test_flags_and_environment_become_the_configuration(workspace, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("seawall.server.serve.run_server", lambda config, **kw: seen.update(config=config, **kw))
    result = serve(
        "--workspace", str(workspace), "--host", "127.0.0.1", "--port", "9123", "--db", str(tmp_path / "x.db"),
        "--max-concurrent-runs", "3", "--max-queued-runs", "5", "--max-loaded-sessions", "7",
        "--max-budget-usd", "1.5", "--max-total-tokens", "5000", "--max-seconds", "90", "--max-turns", "12",
        "--max-message-chars", "2000",
        "--approval-timeout", "20", "--run-timeout", "600", "--allow-full-auto", "--log-level", "WARNING",
        env={TOKEN_ENV: TOKEN},
    )
    assert result.exit_code == 0, result.output
    config = seen["config"]
    assert config.workspace_root == workspace.resolve() and config.port == 9123 and config.token == TOKEN
    assert (config.max_concurrent_runs, config.max_queued_runs, config.max_loaded_sessions) == (3, 5, 7)
    assert (config.max_budget_usd, config.max_total_tokens, config.max_seconds, config.max_turns) == (1.5, 5000, 90, 12)
    assert config.approval_timeout_seconds == 20 and config.run_timeout_seconds == 600
    assert config.max_message_chars == 2000
    assert config.allow_full_auto is True and config.allow_no_auth is False
    assert seen["log_level"] == "warning"
    assert "WARNING" in result.output and "no sandbox" in result.output  # the banner does not hide the risks
    assert TOKEN not in result.output


def test_the_token_variable_name_can_be_chosen(workspace, monkeypatch):
    seen = {}
    monkeypatch.setattr("seawall.server.serve.run_server", lambda config, **kw: seen.update(config=config))
    result = serve("--workspace", str(workspace), "--token-env", "MY_SERVER_TOKEN", env={"MY_SERVER_TOKEN": TOKEN})
    assert result.exit_code == 0 and seen["config"].token == TOKEN


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_real_process_serves_a_conversation_and_shuts_down_on_sigterm(tmp_path, workspace):
    """``python -m seawall serve`` against a scripted model speaking the real Anthropic wire protocol."""
    home = tmp_path / "home"
    home.mkdir()
    port = free_port()
    with ScriptedServer() as model:
        base_url = model.register("e2e", [Turn(text="first answer"), Turn(text="second answer")])
        env = clean_env(
            home,
            extra={
                "SEAWALL_CONFIG_DIR": str(home / ".seawall"),
                "SEAWALL_DATA_DIR": str(home / ".seawall" / "data"),
                "ANTHROPIC_API_KEY": "sk-scripted",
                "ANTHROPIC_BASE_URL": base_url,
                TOKEN_ENV: TOKEN,
                **LOOPBACK_DIRECT,  # the model is on this machine; do not let a system proxy answer for it
            },
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "seawall", "serve", "--workspace", str(workspace), "--port", str(port),
             "--db", str(tmp_path / "e2e.db"), "--log-level", "warning"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"

            def healthy() -> bool:
                try:
                    return httpx.get(f"{base}/healthz", timeout=1, trust_env=False).status_code == 200
                except httpx.HTTPError:
                    return False

            wait_for(healthy, timeout=30, what="the server process to come up")
            http = httpx.Client(
                base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30, verify=False, trust_env=False
            )
            assert httpx.get(f"{base}/v1/sessions", timeout=5, trust_env=False).status_code == 401

            session = http.post("/v1/sessions", json={"title": "e2e"}).json()
            first = http.post(f"/v1/sessions/{session['id']}/messages?wait=1", json={"text": "hello"}).json()
            second = http.post(f"/v1/sessions/{session['id']}/messages?wait=1", json={"text": "and again"}).json()
            assert (first["stop_reason"], first["text"]) == ("completed", "first answer")
            assert (second["stop_reason"], second["text"]) == ("completed", "second answer")
            assert http.get(f"/v1/sessions/{session['id']}").json()["usage"]["input_tokens"] > 0

            # an event stream is open when SIGTERM arrives; the process must still exit promptly
            closed = threading.Event()

            def follow() -> None:
                try:
                    with http.stream("GET", f"/v1/sessions/{session['id']}/events?after=999999") as stream:
                        for _ in stream.iter_lines():
                            pass
                except httpx.HTTPError:
                    pass
                closed.set()

            threading.Thread(target=follow, daemon=True).start()
            wait_for(lambda: http.get("/v1/status").json()["subscribers"] == 1, what="the stream to connect")
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=20)
            # uvicorn finishes a graceful shutdown and then re-raises the signal, so the status
            # is "ended by SIGTERM" (or 0 where it does not do that)
            assert code in (0, -signal.SIGTERM), process.stderr.read()
            assert time.monotonic() - started < 8
            assert closed.wait(5)
            # it really was graceful: the cached runtime was closed, which ends the audit log
            log = home / ".seawall" / "data" / "audit" / f"{session['id']}.jsonl"
            assert '"type":"session.end"' in log.read_text().strip().splitlines()[-1].replace(" ", "")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
