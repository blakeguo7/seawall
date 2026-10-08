"""Prices, the cost tracker and the metering client."""

from __future__ import annotations

import pytest

from seawall.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from seawall.api.usage import UsageSnapshot
from seawall.config.settings import ModelPriceConfig, Settings
from seawall.engine.cost_report import format_cost_report
from seawall.engine.cost_tracker import CostTracker
from seawall.engine.messages import ConversationMessage, TextBlock
from seawall.engine.metering import MeteredApiClient
from seawall.engine.pricing import ModelPrice, PriceTable, normalize_model_name
from seawall.engine.query_engine import QueryEngine
from seawall.permissions import PermissionChecker
from seawall.config.settings import PermissionSettings
from seawall.tools import create_default_tool_registry
from tests.fakes import ScriptedClient, text_message


def usage(i=0, o=0, cw=0, cr=0) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr
    )


# --- prices ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-4-6", (3, 15)),
        ("claude-sonnet-4-5-20250929", (3, 15)),
        ("claude-opus-4-5", (5, 25)),  # the longer prefix wins over claude-opus-4
        ("claude-opus-4-1-20250805", (15, 75)),
        ("claude-haiku-4-5", (1, 5)),
        ("gpt-4o-mini-2024-07-18", (0.15, 0.6)),
        ("gpt-4o", (2.5, 10)),
        ("anthropic/claude-sonnet-4-6", (3, 15)),
        ("Claude-Sonnet-4-6[1m]", (3, 15)),
    ],
)
def test_default_prices_match_by_longest_prefix(model: str, expected: tuple[float, float]) -> None:
    price = PriceTable().lookup(model)
    assert price is not None
    assert (price.input, price.output) == expected


@pytest.mark.parametrize(
    "model", ["deepseek-v4-pro[1m]", "qwen-max", "some-new-model", "claude-sonnet-5-5", ""]
)
def test_models_without_a_price_are_unpriced_not_guessed(model: str) -> None:
    assert PriceTable().lookup(model) is None
    assert PriceTable().cost(model, usage(i=1000)) is None


def test_configured_prices_beat_the_built_in_table() -> None:
    table = PriceTable({"claude-sonnet-4": ModelPrice(1, 2), "deepseek": ModelPrice(0.3, 1.1)})
    assert table.lookup("claude-sonnet-4-6") == ModelPrice(1, 2)
    assert table.cost("deepseek-v4-pro", usage(i=1_000_000, o=1_000_000)) == pytest.approx(1.4)


def test_price_tables_come_from_settings() -> None:
    settings = Settings(pricing={"my-model": ModelPriceConfig(input=2, output=4, cache_read=0.2)})
    table = PriceTable.from_settings(settings.pricing)
    assert table.cost("my-model-v2", usage(i=1_000_000, o=1_000_000, cr=1_000_000)) == pytest.approx(6.2)


def test_cost_arithmetic_with_cache_defaults() -> None:
    price = ModelPrice(input=10, output=20)  # cache write 12.5, read 1.0
    cost = price.cost(usage(i=1_000_000, o=500_000, cw=1_000_000, cr=2_000_000))
    assert cost == pytest.approx(10 + 10 + 12.5 + 2.0)


def test_explicit_cache_prices_are_used() -> None:
    price = ModelPrice(input=10, output=20, cache_write=11, cache_read=0.5)
    assert price.cost(usage(cw=1_000_000, cr=1_000_000)) == pytest.approx(11.5)


def test_model_name_normalisation() -> None:
    assert normalize_model_name("  OpenAI/GPT-4o[beta] ") == "gpt-4o"


# --- tracker ---------------------------------------------------------------------------------


def test_tracker_sums_usage_and_cost_per_model() -> None:
    tracker = CostTracker(PriceTable({"cheap": ModelPrice(1, 1), "dear": ModelPrice(10, 10)}))
    tracker.add(usage(i=1_000_000), model="cheap")
    tracker.add(usage(o=1_000_000), model="dear")
    tracker.add(usage(i=500_000), model="cheap")

    assert tracker.total.input_tokens == 1_500_000
    assert tracker.total.output_tokens == 1_000_000
    assert tracker.cost_usd == pytest.approx(1.5 + 10)
    assert tracker.calls == 3
    assert tracker.unpriced_models == ()
    breakdown = tracker.by_model()
    assert breakdown["cheap"].calls == 2 and breakdown["cheap"].cost_usd == pytest.approx(1.5)
    assert breakdown["dear"].cost_usd == pytest.approx(10)


def test_unpriced_models_are_listed_and_left_out_of_the_cost() -> None:
    tracker = CostTracker(PriceTable({"known": ModelPrice(1, 1)}))
    tracker.add(usage(i=1_000_000), model="known")
    tracker.add(usage(i=9_000_000), model="mystery")

    assert tracker.cost_usd == pytest.approx(1)
    assert tracker.unpriced_models == ("mystery",)
    assert tracker.total.input_tokens == 10_000_000
    assert tracker.by_model()["mystery"].cost_usd is None


def test_tracker_reset_forgets_everything() -> None:
    tracker = CostTracker()
    tracker.add(usage(i=5), model="claude-sonnet-4-6")
    tracker.reset()
    assert tracker.total == UsageSnapshot()
    assert tracker.calls == 0 and tracker.cost_usd == 0 and tracker.unpriced_models == ()


def test_tracker_can_start_from_saved_totals() -> None:
    tracker = CostTracker(PriceTable({"known": ModelPrice(1, 1)}))
    tracker.add(usage(i=99), model="old-model")  # replaced, not added to
    tracker.restore(usage(i=1_000_000, o=500_000), 2.5, unpriced_models=["mystery"])

    assert tracker.total.input_tokens == 1_000_000 and tracker.total.output_tokens == 500_000
    assert tracker.cost_usd == pytest.approx(2.5)
    assert tracker.unpriced_models == ("mystery",)

    tracker.add(usage(i=1_000_000), model="known")  # new calls accumulate on top
    assert tracker.total.input_tokens == 2_000_000
    assert tracker.cost_usd == pytest.approx(3.5)
    assert tracker.unpriced_models == ("mystery",)


def test_usage_arithmetic() -> None:
    total = usage(1, 2, 3, 4).plus(usage(10, 20, 30, 40))
    assert total == usage(11, 22, 33, 44)
    assert total.total_tokens == 33
    assert total.all_tokens == 110


# --- metering -----------------------------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.closed = False
        self.marker = "inner"

    async def stream_message(self, request):
        yield "not a completion event"
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text="hi")]),
            usage=usage(i=7, o=3),
            stop_reason=None,
        )


async def test_the_metered_client_counts_completed_calls_and_passes_events_through() -> None:
    tracker = CostTracker()
    client = MeteredApiClient(_Client(), tracker)
    request = ApiMessageRequest(model="claude-sonnet-4-6", messages=[], max_tokens=10)

    events = [event async for event in client.stream_message(request)]

    assert events[0] == "not a completion event"
    assert isinstance(events[1], ApiMessageCompleteEvent)
    assert tracker.total.input_tokens == 7
    assert tracker.by_model()["claude-sonnet-4-6"].calls == 1
    assert client.marker == "inner"  # everything else is delegated


def make_engine(tmp_path, client) -> QueryEngine:
    return QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="claude-sonnet-4-6",
        system_prompt="system",
    )


async def test_every_model_call_made_through_the_engine_is_counted(tmp_path) -> None:
    """Compaction and memory extraction use the engine's client, so they are metered too."""
    engine = make_engine(tmp_path, ScriptedClient(text_message("a"), text_message("b")))

    _ = [event async for event in engine.submit_message("hello")]
    side_call = engine._metered_client()  # what compaction and memory extraction are handed
    request = ApiMessageRequest(model="claude-sonnet-4-6", messages=[], max_tokens=10)
    _ = [event async for event in side_call.stream_message(request)]

    assert engine.cost_tracker.calls == 2
    assert engine.total_usage.input_tokens == 20  # 10 per scripted call


async def test_clearing_the_conversation_resets_the_meter(tmp_path) -> None:
    engine = make_engine(tmp_path, ScriptedClient(text_message("a")))
    _ = [event async for event in engine.submit_message("hello")]
    tracker = engine.cost_tracker
    engine.clear()
    assert engine.cost_tracker is tracker  # the metered client keeps pointing at the same tracker
    assert tracker.calls == 0


# --- report -------------------------------------------------------------------------------------------


async def test_cost_report(tmp_path) -> None:
    client = ScriptedClient(text_message("a"), usage=usage(i=1_000_000, o=100_000))
    engine = make_engine(tmp_path, client)
    _ = [event async for event in engine.submit_message("hello")]
    report = format_cost_report(engine)

    assert "Model: claude-sonnet-4-6" in report
    assert "Model calls: 1" in report
    assert "Input tokens: 1000000" in report
    assert "Estimated cost: $4.5000" in report  # 1M * $3 + 0.1M * $15


async def test_cost_report_says_when_a_model_has_no_price(tmp_path) -> None:
    engine = make_engine(tmp_path, ScriptedClient(text_message("a")))
    engine.set_model("mystery-model")
    _ = [event async for event in engine.submit_message("hello")]
    report = format_cost_report(engine)

    assert "Estimated cost: unknown" in report
    assert "no price for mystery-model" in report
