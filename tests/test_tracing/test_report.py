"""Reading traces back, with spans written by hand so the arithmetic can be checked."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import seawall.cli as cli
from seawall.config.paths import get_data_dir
from seawall.tracing.report import (
    format_session,
    format_summary,
    format_tree,
    fmt_ms,
    overview,
    read_spans,
    summarize,
)

BASE = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
TRACE = "a" * 32
runner = CliRunner()


def span(name, at, ms, *, id, parent=None, status="ok", trace=TRACE, **attrs) -> dict:
    """A span starting ``at`` seconds after BASE and lasting ``ms``."""
    return {
        "v": 1, "trace": trace, "span": id, "parent": parent, "session": "sess", "name": name,
        "start": (BASE + timedelta(seconds=at)).isoformat(timespec="milliseconds"),
        "ms": ms, "status": status, "attrs": attrs,
    }


def one_run() -> list[dict]:
    """A 10 s run: a 3 s model call, two tool calls side by side (one waits 1 s for approval),
    then a 1 s compaction that makes its own 0.8 s model call."""
    return [
        span("run", 0, 10_000, id="run", stop_reason="completed", turns=1, input_tokens=300, output_tokens=60, cost_usd=0.0123),
        span("turn", 0, 9_000, id="turn", parent="run", index=1),
        span("model.call", 0, 3_000, id="m1", parent="turn", model="m", input_tokens=100, output_tokens=20, cost_usd=0.001, first_text_ms=400.0),
        span("tool.call", 3, 4_000, id="t1", parent="turn", tool="bash", exec_ms=2_900.0, risk="low"),
        span("approval.wait", 3.5, 1_000, id="w1", parent="t1", tool="bash", outcome="approved"),
        span("tool.call", 3, 2_000, id="t2", parent="turn", tool="read_file", status="denied", denied_by="policy"),
        span("compaction", 7, 1_000, id="c1", parent="turn", trigger="auto", compacted=True),
        span("model.call", 7.1, 800, id="m2", parent="c1", model="m", input_tokens=200, output_tokens=40, cost_usd=0.002),
    ]


def test_the_tree_shows_each_step_under_the_one_that_ran_it() -> None:
    lines = format_tree(one_run())

    assert lines[0].startswith("run  10.00s  completed  1 turns  360 tokens  $0.0123")
    assert lines[1].startswith("├─ turn 1  9.00s") or lines[1].startswith("└─ turn 1  9.00s")
    assert any("model.call  3.00s  m  in 100 out 20  first text 400ms  $0.0010" in line for line in lines)
    assert any("tool.call bash  4.00s  ok  ran 2.90s  risk low" in line for line in lines)
    assert any("tool.call read_file  2.00s  denied  by policy" in line for line in lines)
    assert "   │  └─ approval.wait bash  1.00s  approved" in lines  # nested under its tool call
    assert any("compaction auto  1.00s  compacted" in line for line in lines)


def test_the_time_is_split_without_counting_overlaps_twice() -> None:
    summary = summarize(one_run(), TRACE)

    assert summary.total_ms == 10_000
    assert summary.model_ms == 3_000  # the compaction's own call is counted as compaction
    assert summary.compaction_ms == 1_000
    assert summary.approval_ms == pytest.approx(1_000)
    assert summary.tools_ms == pytest.approx(3_000)  # the two calls overlap: 4 s in all, less the 1 s wait
    assert summary.other_ms == pytest.approx(2_000)
    parts = (summary.model_ms, summary.tools_ms, summary.approval_ms, summary.compaction_ms, summary.other_ms)
    assert sum(parts) == pytest.approx(summary.total_ms)  # the five parts add up to the run


def test_the_summary_counts_calls_tokens_and_cost() -> None:
    summary = summarize(one_run(), TRACE)

    assert (summary.model_calls, summary.tool_calls, summary.denied) == (2, 2, 1)
    assert summary.tokens == 360 and summary.cost_usd == pytest.approx(0.003) and not summary.unpriced
    assert summary.slowest[0] == "tool.call bash 4.00s (turn 1)" and "model.call 3.00s (turn 1)" in summary.slowest


def test_failed_steps_are_listed_as_problems() -> None:
    spans = one_run() + [span("tool.call", 8, 50, id="t3", parent="turn", status="error", tool="bash", error="exit 2")]

    summary = summarize(spans, TRACE)

    assert summary.problems == ["tool.call bash 50ms (turn 1): exit 2"]
    assert "problem: tool.call bash 50ms (turn 1): exit 2" in format_summary(summary)


def test_an_unpriced_model_is_said_to_be_unpriced_not_free() -> None:
    spans = [
        span("run", 0, 1_000, id="run", stop_reason="completed", turns=1, input_tokens=5, output_tokens=5, cost_usd=0.0, unpriced_models=["m"]),
        span("model.call", 0, 500, id="m1", parent="run", model="m", input_tokens=5, output_tokens=5, cost_usd=None),
    ]

    assert "unpriced" in format_tree(spans)[0]
    assert any("unpriced" in line for line in format_summary(summarize(spans, TRACE)))


def test_durations_are_written_for_people() -> None:
    assert [fmt_ms(ms) for ms in (12, 999, 1_500, 59_999, 61_000, 125_000)] == ["12ms", "999ms", "1.50s", "60.00s", "1m01s", "2m05s"]


def test_a_line_cut_short_by_a_crash_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    good = json.dumps(one_run()[0])
    path.write_text(f"{good}\nnot json at all\n{{\"span\": \"half\n")

    assert len(read_spans(path)) == 1


def test_a_session_shows_its_last_run_by_default_and_any_run_on_request(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    second = [{**s, "trace": "b" * 32, "start": (BASE + timedelta(hours=1)).isoformat(timespec="milliseconds")} for s in one_run()]
    path.write_text("".join(json.dumps(s) + "\n" for s in one_run() + second))

    assert format_session(path).startswith("run 2 of 2")
    assert format_session(path, run=1).startswith("run 1 of 2")
    assert "there is no run 3" in format_session(path, run=3)
    everything = format_session(path, all_runs=True)
    assert "run 1 of 2" in everything and "run 2 of 2" in everything


def test_model_calls_made_outside_any_run_are_reported_not_lost(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    extraction = span("model.call", 20, 700, id="x1", trace="c" * 32, model="m", input_tokens=50, output_tokens=10, cost_usd=0.0004)
    path.write_text("".join(json.dumps(s) + "\n" for s in [*one_run(), extraction]))

    text = format_session(path)

    assert "outside any run: 1 model calls (for example memory), 700ms, 60 tokens, $0.0004" in text


def test_an_overview_row_has_what_a_listing_needs(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(s) + "\n" for s in one_run()))

    row = overview(path)

    assert (row.session, row.runs, row.last_stop, row.total_ms) == ("sess", 1, "completed", 10_000)
    assert row.cost_usd == pytest.approx(0.0123)
    assert overview(tmp_path / "missing.jsonl") is None


# --- the commands ----------------------------------------------------------------------------


def write_session(session: str = "abc123def456") -> Path:
    path = get_data_dir() / "traces" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({**s, "session": session}) + "\n" for s in one_run()))
    return path


def test_trace_list_says_so_when_there_is_nothing() -> None:
    result = runner.invoke(cli.app, ["trace", "list"])

    assert result.exit_code == 0 and "No traces in" in result.output


def test_trace_list_and_show_find_a_session_by_prefix() -> None:
    write_session()

    listing = runner.invoke(cli.app, ["trace", "list"])
    shown = runner.invoke(cli.app, ["trace", "show", "abc1"])

    assert listing.exit_code == 0 and "abc123def456" in listing.output and "last: completed" in listing.output
    assert shown.exit_code == 0 and "tool.call bash" in shown.output and "where the time went (10.00s)" in shown.output


def test_trace_show_can_print_the_raw_spans() -> None:
    write_session()

    result = runner.invoke(cli.app, ["trace", "show", "abc123", "--json"])

    lines = [json.loads(line) for line in result.output.splitlines()]
    assert result.exit_code == 0 and len(lines) == len(one_run()) and lines[0]["name"] == "run"


def test_trace_show_for_an_unknown_session_fails_clearly() -> None:
    result = runner.invoke(cli.app, ["trace", "show", "nope"])

    assert result.exit_code == 2
