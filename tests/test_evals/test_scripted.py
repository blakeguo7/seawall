"""The scripted model server, exercised through the project's real Anthropic client."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from seawall.api.client import AnthropicApiClient, ApiMessageCompleteEvent, ApiMessageRequest, ApiTextDeltaEvent
from seawall.engine.messages import ConversationMessage, TextBlock, ToolResultBlock
from seawall.evals.scripted import (
    SCRIPTED_AGENTS,
    SCRIPTED_MODEL,
    ScriptedServer,
    ToolCall,
    Turn,
    oracle_script,
)
from seawall.evals.task import load_task
from tests.test_evals.conftest import make_task


@pytest.fixture
def server():
    with ScriptedServer() as running:
        yield running


async def ask(base_url: str, messages: list[ConversationMessage]):
    client = AnthropicApiClient(api_key="sk-test", base_url=base_url)
    request = ApiMessageRequest(model=SCRIPTED_MODEL, messages=messages, max_tokens=100)
    return [event async for event in client.stream_message(request)]


async def test_the_client_parses_a_scripted_tool_call(server) -> None:
    script = [
        Turn(text="Looking.", tool_calls=(ToolCall("read_file", {"path": "a.py"}),)),
        Turn(text="All done."),
    ]
    url = server.register("run1", script)

    events = await ask(url, [ConversationMessage.from_user_text("hi")])
    final = next(e for e in events if isinstance(e, ApiMessageCompleteEvent))
    assert [e.text for e in events if isinstance(e, ApiTextDeltaEvent)] == ["Looking."]
    (tool_use,) = final.message.tool_uses
    assert (tool_use.name, tool_use.input) == ("read_file", {"path": "a.py"})
    assert final.usage.input_tokens > 0 and final.usage.output_tokens > 0


async def test_the_reply_follows_the_number_of_assistant_turns_so_far(server) -> None:
    url = server.register("run2", [Turn(tool_calls=(ToolCall("bash", {"command": "ls"}),)), Turn(text="second"), Turn(text="third")])
    user = ConversationMessage.from_user_text("hi")
    first = await ask(url, [user])
    tool_use = next(e for e in first if isinstance(e, ApiMessageCompleteEvent)).message
    results = ConversationMessage(
        role="user", content=[ToolResultBlock(tool_use_id=tool_use.tool_uses[0].id, content="out")]
    )

    second = await ask(url, [user, tool_use, results])
    assert next(e for e in second if isinstance(e, ApiMessageCompleteEvent)).message.text == "second"
    # asking again with the same history gives the same answer: retries are harmless
    again = await ask(url, [user, tool_use, results])
    assert next(e for e in again if isinstance(e, ApiMessageCompleteEvent)).message.text == "second"

    third = await ask(url, [user, tool_use, results, ConversationMessage(role="assistant", content=[TextBlock(text="second")]), results])
    assert next(e for e in third if isinstance(e, ApiMessageCompleteEvent)).message.text == "third"


async def test_past_the_end_of_the_script_the_server_says_so(server) -> None:
    url = server.register("run3", [])
    events = await ask(url, [ConversationMessage.from_user_text("hi")])
    assert next(e for e in events if isinstance(e, ApiMessageCompleteEvent)).message.text == "(end of script)"


def test_requests_are_recorded_per_run(server) -> None:
    url = server.register("run4", [Turn(text="x")])
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{url}/v1/messages", data=body, headers={"content-type": "application/json"}))
    recorded = server.requests_for("run4")
    assert len(recorded) == 1 and recorded[0]["messages"][0]["content"] == "hello"
    assert server.requests_for("unknown") == []


def test_non_streaming_requests_get_a_json_message(server) -> None:
    url = server.register("run5", [Turn(text="plain")])
    body = json.dumps({"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}).encode()
    response = urllib.request.urlopen(urllib.request.Request(f"{url}/v1/messages", data=body, headers={"content-type": "application/json"}))
    message = json.loads(response.read())
    assert message["content"] == [{"type": "text", "text": "plain"}] and message["stop_reason"] == "end_turn"


def test_unknown_runs_are_a_404(server) -> None:
    body = b"{}"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(urllib.request.Request(f"{server.address}/nope/v1/messages", data=body))
    assert excinfo.value.code == 404


def test_scripts_built_from_a_task(tasks_root: Path) -> None:
    make_task(tasks_root, protected=[], repo={"calc.py": "x\n", "tests/test_calc.py": "y\n"})
    task = load_task(tasks_root / "demo")

    oracle = oracle_script(task)
    writes = oracle[0].tool_calls
    assert [c.name for c in writes] == ["write_file"]
    assert writes[0].input == {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}
    assert oracle[1].tool_calls[0].name == "bash" and oracle[-1].tool_calls == ()

    noop = SCRIPTED_AGENTS["noop"](task)
    assert all(c.name not in {"write_file", "edit_file", "bash"} for t in noop for c in t.tool_calls)

    cheater = SCRIPTED_AGENTS["cheater"](task)
    assert cheater[0].tool_calls[0].name == "write_file"
