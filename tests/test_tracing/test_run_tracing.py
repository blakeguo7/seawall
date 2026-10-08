"""A real run through the agent loop, with a scripted model, leaves the trace it should."""

from __future__ import annotations

from pathlib import Path

import pytest

from seawall.api.usage import UsageSnapshot
from seawall.config.settings import ModelPriceConfig, PermissionSettings
from seawall.engine.cost_tracker import CostTracker
from seawall.engine.messages import ConversationMessage, TextBlock
from seawall.engine.metering import MeteredApiClient
from seawall.engine.pricing import PriceTable
from seawall.engine.query import MaxTurnsExceeded, QueryContext, run_query
from seawall.permissions import PermissionChecker, PermissionMode
from seawall.tools import create_default_tool_registry
from seawall.tracing import Tracer
from seawall.tracing.report import format_session, read_spans
from tests.fakes import ScriptedClient, text_message, tool_message


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(tmp_path / "traces" / "sess.jsonl", session_id="sess")


async def drive(
    tmp_path: Path,
    tracer: Tracer,
    client,
    *,
    mode: PermissionMode = PermissionMode.FULL_AUTO,
    max_turns: int = 5,
    prices: PriceTable | None = None,
) -> list[dict]:
    """Run one prompt and return the spans it left behind."""
    cost = CostTracker(prices)
    context = QueryContext(
        api_client=MeteredApiClient(client, cost, tracer),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=mode)),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_tokens=100,
        max_turns=max_turns,
        cost=cost,
        tracer=tracer,
        tool_metadata={"session_id": "sess"},  # the engine always passes one
    )
    try:
        async for _ in run_query(context, [ConversationMessage(role="user", content=[TextBlock(text="go")])]):
            pass
    except MaxTurnsExceeded:
        pass
    return read_spans(tracer.path)


def named(spans: list[dict], name: str) -> list[dict]:
    return [s for s in spans if s["name"] == name]


async def test_a_run_is_a_tree_of_turns_model_calls_and_tool_calls(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "echo hi"})), text_message("done"))

    spans = await drive(tmp_path, tracer, client)

    (run,) = named(spans, "run")
    turns, calls, (tool,) = named(spans, "turn"), named(spans, "model.call"), named(spans, "tool.call")
    assert run["parent"] is None and len({s["trace"] for s in spans}) == 1
    assert [t["parent"] for t in turns] == [run["span"]] * 2
    assert sorted(c["parent"] for c in calls) == sorted(t["span"] for t in turns)  # one call per turn
    assert tool["parent"] == next(t["span"] for t in turns if t["attrs"]["index"] == 1)
    assert tool["attrs"]["tool"] == "bash" and tool["attrs"]["input"] == {"command": "echo hi"}
    assert tool["status"] == "ok" and tool["attrs"]["exec_ms"] >= 0 and tool["attrs"]["risk"]
    assert (run["status"], run["attrs"]["stop_reason"], run["attrs"]["turns"]) == ("ok", "completed", 2)
    assert "compaction" not in {s["name"] for s in spans}  # the check found nothing to do


async def test_the_run_span_counts_only_what_that_run_used(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "true"})), text_message("done"), usage=UsageSnapshot(input_tokens=10, output_tokens=5))

    spans = await drive(tmp_path, tracer, client)

    (run,) = named(spans, "run")
    assert (run["attrs"]["input_tokens"], run["attrs"]["output_tokens"]) == (20, 10)  # two calls of 10 and 5


async def test_a_model_call_records_usage_and_its_price(tmp_path: Path, tracer: Tracer) -> None:
    prices = PriceTable.from_settings({"test-model": ModelPriceConfig(input=3, output=15)})

    priced = await drive(tmp_path, tracer, ScriptedClient(text_message("hi")), prices=prices)
    unpriced = await drive(tmp_path, Tracer(tmp_path / "u.jsonl", session_id="u"), ScriptedClient(text_message("hi")))

    (call,) = named(priced, "model.call")
    assert call["attrs"]["model"] == "test-model" and call["attrs"]["tool_calls"] == 0
    assert (call["attrs"]["input_tokens"], call["attrs"]["output_tokens"]) == (10, 5)
    assert call["attrs"]["cost_usd"] == pytest.approx((10 * 3 + 5 * 15) / 1_000_000)
    assert call["attrs"]["first_text_ms"] is not None  # the reply had text
    assert named(unpriced, "model.call")[0]["attrs"]["cost_usd"] is None  # no price: not guessed


async def test_tool_calls_of_one_turn_hang_under_that_turn(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "echo a"}), ("bash", {"command": "echo b"})), text_message("done"))

    spans = await drive(tmp_path, tracer, client)

    first_turn = next(t for t in named(spans, "turn") if t["attrs"]["index"] == 1)
    tools = named(spans, "tool.call")
    assert len(tools) == 2 and {t["parent"] for t in tools} == {first_turn["span"]}


async def test_a_refused_call_is_denied_and_shows_the_wait_for_approval(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "mkdir made"})), text_message("ok"))

    spans = await drive(tmp_path, tracer, client, mode=PermissionMode.DEFAULT)  # nobody to approve

    (tool,) = named(spans, "tool.call")
    (wait,) = named(spans, "approval.wait")
    assert (tool["status"], tool["attrs"]["denied_by"]) == ("denied", "approval")
    assert wait["parent"] == tool["span"] and wait["attrs"]["outcome"] == "unavailable"
    assert not (tmp_path / "made").exists()


async def test_a_failing_model_call_is_an_error_all_the_way_up(tmp_path: Path, tracer: Tracer) -> None:
    class Failing:
        async def stream_message(self, request):
            raise RuntimeError("provider is down")
            yield  # makes this an async generator

    spans = await drive(tmp_path, tracer, Failing())

    (call,) = named(spans, "model.call")
    (run,) = named(spans, "run")
    assert call["status"] == "error" and call["attrs"]["error"] == "RuntimeError: provider is down"
    assert (run["status"], run["attrs"]["stop_reason"]) == ("error", "error")


async def test_a_run_cut_short_by_a_limit_is_stopped_not_failed(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "true"})), text_message("never reached"))

    spans = await drive(tmp_path, tracer, client, max_turns=1)

    (run,) = named(spans, "run")
    assert (run["status"], run["attrs"]["stop_reason"]) == ("stopped", "max_turns")


async def test_message_text_is_kept_out_unless_asked_for(tmp_path: Path) -> None:
    quiet = Tracer(tmp_path / "q.jsonl", session_id="q")
    loud = Tracer(tmp_path / "l.jsonl", session_id="l", capture_content=True)
    script = lambda: ScriptedClient(tool_message(("bash", {"command": "echo secret-output"})), text_message("the answer"))  # noqa: E731

    quiet_spans = await drive(tmp_path, quiet, script())
    loud_spans = await drive(tmp_path, loud, script())

    assert "output_preview" not in {k for s in quiet_spans for k in s["attrs"]}
    assert any(s["attrs"].get("output_preview") == "the answer" for s in named(loud_spans, "model.call"))
    assert any("secret-output" in s["attrs"].get("output_preview", "") for s in named(loud_spans, "tool.call"))


async def test_a_traced_run_reads_back_as_a_tree(tmp_path: Path, tracer: Tracer) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "echo hi"})), text_message("done"))
    await drive(tmp_path, tracer, client)

    text = format_session(tracer.path)

    lines = text.splitlines()
    assert lines[0].startswith("run 1 of 1")
    assert any(line.startswith("run  ") and "completed" in line and "2 turns" in line for line in lines)
    assert any("tool.call bash" in line for line in lines) and any("model.call" in line for line in lines)
    assert any(line.startswith("where the time went") for line in lines)
