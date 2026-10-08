"""A scripted stand-in for the model, served over the Anthropic Messages API.

The eval harness runs the real ``seawall`` command against this server, so a trial exercises the
whole stack (HTTP client, streaming parser, agent loop, tools, permissions, audit log) with a
model whose answers are fixed in advance. That makes the suites reproducible and free:

* the *oracle* agent applies the task's reference solution, so a passing oracle run shows that
  the harness, the tools and the grader all work;
* the *no-op* agent changes nothing, so a failing no-op run shows that grading cannot be passed
  by accident;
* the *cheater* rewrites the tests and the *wrongfix* agent edits the code without fixing it, so
  each is a failure of a known kind and the attribution of failures can be checked against them;
* the safety suite plays a compromised model that tries harmful calls, to check that the
  harness refuses them.

The server picks each reply from the number of assistant turns already in the request, so it
needs no per-connection state and retries are harmless.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

from seawall.evals.task import Task

SCRIPTED_MODEL = "scripted-model"


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    """One assistant message: some text, some tool calls, or both."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


Script = Sequence[Turn]


# ---------------------------------------------------------------------------
# Scripts built from a task
# ---------------------------------------------------------------------------


def oracle_script(task: Task) -> list[Turn]:
    """Apply the reference solution file by file, run the visible tests, and report."""
    writes = tuple(
        ToolCall("write_file", {"path": path.relative_to(task.solution_dir).as_posix(), "content": path.read_text()})
        for path in sorted(task.solution_dir.rglob("*"))
        if path.is_file()
    )
    return [
        Turn(text="I found the problem and I'm fixing it.", tool_calls=writes),
        Turn(tool_calls=(ToolCall("bash", {"command": "python -m pytest -q -p no:cacheprovider"}),)),
        Turn(text="The fix is in place and the tests pass."),
    ]


def noop_script(task: Task) -> list[Turn]:
    """An agent that looks and then does nothing."""
    return [
        Turn(tool_calls=(ToolCall("glob", {"pattern": "**/*.py"}),)),
        Turn(text="I looked at the code but made no changes."),
    ]


def cheater_script(task: Task) -> list[Turn]:
    """An agent that tries to pass by rewriting the tests instead of fixing the code."""
    protected = [pattern for pattern in task.protected if "*" not in pattern] or ["tests/test_cheat.py"]
    writes = tuple(
        ToolCall("write_file", {"path": path, "content": "def test_nothing():\n    assert True\n"})
        for path in protected
    )
    return [
        Turn(text="Simplest way to make the tests green.", tool_calls=writes),
        Turn(text="Done, all tests pass."),
    ]


def wrongfix_script(task: Task) -> list[Turn]:
    """An agent that edits the files the solution touches without fixing anything, runs the tests,
    sees them fail and says so."""
    writes = []
    for path in sorted(task.solution_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(task.solution_dir)
        original = task.repo_dir / relative
        before = original.read_text() if original.is_file() else ""
        writes.append(ToolCall("write_file", {"path": relative.as_posix(), "content": "# attempted fix\n" + before}))
    return [
        Turn(text="I think I see it, trying a change.", tool_calls=tuple(writes)),
        Turn(tool_calls=(ToolCall("bash", {"command": "python -m pytest -q -p no:cacheprovider"}),)),
        Turn(text="The tests still fail and I could not find out why."),
    ]


SCRIPTED_AGENTS = {
    "oracle": oracle_script,
    "noop": noop_script,
    "cheater": cheater_script,
    "wrongfix": wrongfix_script,
}


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class ScriptedServer:
    """A local Anthropic-compatible endpoint that replays registered scripts.

    ``register(run_id, script)`` returns the base URL for one trial: point ``seawall --base-url`` at
    it and every model call of that trial gets the next scripted turn.
    """

    def __init__(self) -> None:
        self._scripts: dict[str, list[Turn]] = {}
        self._requests: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                outer._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> ScriptedServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> ScriptedServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def address(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def register(self, run_id: str, script: Script) -> str:
        with self._lock:
            self._scripts[run_id] = list(script)
            self._requests[run_id] = []
        return f"{self.address}/{run_id}"

    def requests_for(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._requests.get(run_id, []))

    # --- request handling ----------------------------------------------------------------

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = handler.path.strip("/").split("/")
        run_id = parts[0] if parts else ""
        length = int(handler.headers.get("content-length", 0) or 0)
        try:
            body = json.loads(handler.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(handler, 400, {"error": "bad json"})
        with self._lock:
            script = self._scripts.get(run_id)
            if script is None or parts[-2:] != ["v1", "messages"]:
                return self._reply(handler, 404, {"error": f"no script for {handler.path}"})
            self._requests[run_id].append(body)
        turn_index = sum(1 for message in body.get("messages", []) if message.get("role") == "assistant")
        turn = script[turn_index] if turn_index < len(script) else Turn(text="(end of script)")
        input_tokens = max(1, (len(json.dumps(body.get("messages", []))) + len(str(body.get("system", "")))) // 4)
        message = _message_for(turn, run_id, turn_index, input_tokens)
        if body.get("stream"):
            payload = _sse(message)
            self._reply(handler, 200, payload, content_type="text/event-stream")
        else:
            self._reply(handler, 200, message["final"])

    @staticmethod
    def _reply(handler: BaseHTTPRequestHandler, status: int, payload: Any, content_type: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


def _message_for(turn: Turn, run_id: str, index: int, input_tokens: int) -> dict[str, Any]:
    """The reply for ``turn``: content blocks, usage, and the final (non-streaming) message."""
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for number, call in enumerate(turn.tool_calls, 1):
        blocks.append(
            {"type": "tool_use", "id": f"toolu_{run_id}_{index}_{number}", "name": call.name, "input": call.input}
        )
    output_tokens = max(1, len(json.dumps(blocks)) // 4)
    stop_reason = "tool_use" if turn.tool_calls else "end_turn"
    final = {
        "id": f"msg_{run_id}_{index}",
        "type": "message",
        "role": "assistant",
        "model": SCRIPTED_MODEL,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    return {
        "blocks": blocks,
        "final": final,
        "stop_reason": stop_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _sse(message: dict[str, Any]) -> bytes:
    """Encode a reply as the server-sent events the Messages API streams."""
    final = message["final"]
    started = {**final, "content": [], "stop_reason": None,
               "usage": {"input_tokens": message["input_tokens"], "output_tokens": 1}}
    events: list[tuple[str, dict[str, Any]]] = [("message_start", {"type": "message_start", "message": started})]
    for index, block in enumerate(message["blocks"]):
        if block["type"] == "text":
            opening = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            opening = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            ("content_block_start", {"type": "content_block_start", "index": index, "content_block": opening}),
            ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
    ending = {"stop_reason": message["stop_reason"], "stop_sequence": None}
    events += [
        ("message_delta", {"type": "message_delta", "delta": ending, "usage": {"output_tokens": message["output_tokens"]}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
