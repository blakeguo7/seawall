"""The tracer: nesting, what a record holds, and what happens when things go wrong."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from seawall.config.settings import TraceSettings
from seawall.tracing import NULL_SPAN, NULL_TRACER, Tracer, open_session_trace, resolve_trace
from seawall.tracing.report import read_spans


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(tmp_path / "traces" / "sess.jsonl", session_id="sess", max_field_chars=80)


def spans_of(tracer: Tracer) -> list[dict]:
    return read_spans(tracer.path)


def test_a_record_has_the_fields_of_an_opentelemetry_span(tracer: Tracer) -> None:
    with tracer.start("run", model="m"):
        pass

    (record,) = spans_of(tracer)

    assert record["v"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", record["trace"]) and re.fullmatch(r"[0-9a-f]{16}", record["span"])
    assert record["parent"] is None and record["session"] == "sess" and record["name"] == "run"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", record["start"])
    assert record["ms"] >= 0 and record["status"] == "ok" and record["attrs"] == {"model": "m"}


def test_a_span_started_under_the_current_one_becomes_its_child(tracer: Tracer) -> None:
    run = tracer.start("run", make_current=True)
    call = tracer.start("model.call")
    call.end()
    run.end()

    child, parent = spans_of(tracer)

    assert child["parent"] == parent["span"] and child["trace"] == parent["trace"]
    assert tracer.current is None  # ending the run handed the default parent back


def test_an_explicit_parent_wins_over_the_current_span(tracer: Tracer) -> None:
    """Tool calls of one turn run side by side: each names the turn, not whichever span is current."""
    turn = tracer.start("turn", make_current=True)
    other = tracer.start("compaction", make_current=True)
    tool = tracer.start("tool.call", parent=turn)
    tool.end()
    other.end()
    turn.end()

    by_name = {s["name"]: s for s in spans_of(tracer)}

    assert by_name["tool.call"]["parent"] == by_name["turn"]["span"]
    assert by_name["compaction"]["parent"] == by_name["turn"]["span"]


def test_spans_with_nothing_current_start_separate_traces(tracer: Tracer) -> None:
    tracer.start("model.call").end()
    tracer.start("model.call").end()

    first, second = spans_of(tracer)

    assert first["trace"] != second["trace"]


def test_ending_a_span_twice_writes_it_once(tracer: Tracer) -> None:
    span = tracer.start("run")
    span.end()
    span.end("error")

    assert [s["status"] for s in spans_of(tracer)] == ["ok"]


def test_leaving_a_with_block_by_an_exception_marks_the_span(tracer: Tracer) -> None:
    with pytest.raises(ValueError):
        with tracer.start("tool.call"):
            raise ValueError("boom")
    with pytest.raises(asyncio.CancelledError):
        with tracer.start("tool.call"):
            raise asyncio.CancelledError

    failed, cancelled = spans_of(tracer)

    assert (failed["status"], failed["attrs"]["error"]) == ("error", "ValueError: boom")
    assert cancelled["status"] == "cancelled"


def test_a_discarded_span_is_not_written_and_gives_back_the_current_span(tracer: Tracer) -> None:
    run = tracer.start("run", make_current=True)
    check = tracer.start("compaction", make_current=True)
    check.discard()
    assert tracer.current is run
    run.end()

    assert [s["name"] for s in spans_of(tracer)] == ["run"]


def test_credentials_are_masked_and_long_text_is_cut(tracer: Tracer) -> None:
    tracer.start("tool.call", note="key sk-" + "a" * 30 + " here", long="x" * 500).end()

    (record,) = spans_of(tracer)

    assert "sk-aaaa" not in record["attrs"]["note"] and "[REDACTED]" in record["attrs"]["note"]
    assert len(record["attrs"]["long"]) < 120 and "+420 chars" in record["attrs"]["long"]


def test_the_file_is_private_to_the_user(tracer: Tracer) -> None:
    tracer.start("run").end()

    assert tracer.path.stat().st_mode & 0o777 == 0o600


def test_a_trace_file_that_cannot_be_written_never_stops_the_agent(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the directory should be")
    tracer = Tracer(blocker / "sess.jsonl", session_id="sess")

    tracer.start("run").end()  # logs a warning and carries on
    tracer.start("run").end()

    assert not (blocker / "sess.jsonl").exists()


def test_a_disabled_tracer_writes_nothing_and_every_span_is_inert(tmp_path: Path) -> None:
    settings = TraceSettings(enabled=False, directory=str(tmp_path / "t"))

    tracer = open_session_trace(settings, "sess")
    with tracer.start("run", make_current=True) as span:
        span.set(a=1)
        span.child("turn").end()

    assert tracer is NULL_TRACER and span is NULL_SPAN
    assert not (tmp_path / "t").exists()


def test_a_session_gets_its_own_file_in_the_configured_directory(tmp_path: Path) -> None:
    settings = TraceSettings(directory=str(tmp_path / "t"), capture_content=True)

    tracer = open_session_trace(settings, "abc123")
    tracer.start("run").end()

    assert tracer.path == tmp_path / "t" / "abc123.jsonl" and tracer.capture_content is True
    assert resolve_trace(tmp_path / "t", "abc") == tracer.path  # a prefix is enough
    assert resolve_trace(tmp_path / "t", "zzz") is None
