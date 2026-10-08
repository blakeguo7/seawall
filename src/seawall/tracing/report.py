"""Reading traces back: the span tree of a run, and where its time and money went."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

Span = dict[str, Any]


def read_spans(path: Path) -> list[Span]:
    """Every span in a trace file. A line cut short by a crash is skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans: list[Span] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "span" in record and "name" in record and "trace" in record:
            spans.append(record)
    return spans


def runs(spans: list[Span]) -> list[Span]:
    """The ``run`` spans, in the order they started."""
    return sorted((s for s in spans if s["name"] == "run" and s.get("parent") is None), key=lambda s: s["start"])


def fmt_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f}s"
    return f"{int(ms // 60_000)}m{(ms % 60_000) / 1000:02.0f}s"


def _epoch(span: Span) -> float:
    return datetime.fromisoformat(span["start"]).timestamp()


def _attrs(span: Span) -> dict[str, Any]:
    attrs = span.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _cost(value: Any) -> str:
    return "unpriced" if value is None else f"${value:.4f}"


def _describe(span: Span) -> str:
    attrs, name, status, dur = _attrs(span), span["name"], span.get("status", "ok"), fmt_ms(span.get("ms", 0))
    if name == "run":
        tokens = (attrs.get("input_tokens") or 0) + (attrs.get("output_tokens") or 0)
        cost = "unpriced" if attrs.get("unpriced_models") else _cost(attrs.get("cost_usd"))
        parts = [f"run  {dur}", str(attrs.get("stop_reason", status)), f"{attrs.get('turns', 0)} turns", f"{tokens:,} tokens", cost]
    elif name == "turn":
        parts = [f"turn {attrs.get('index', '?')}  {dur}"]
    elif name == "model.call":
        parts = [f"model.call  {dur}", str(attrs.get("model", "?"))]
        if "input_tokens" in attrs:
            parts.append(f"in {attrs['input_tokens']:,} out {attrs.get('output_tokens', 0):,}")
        if attrs.get("first_text_ms") is not None:
            parts.append(f"first text {fmt_ms(attrs['first_text_ms'])}")
        if attrs.get("retries"):
            parts.append(f"{attrs['retries']} retries")
        if "cost_usd" in attrs:
            parts.append(_cost(attrs["cost_usd"]))
    elif name == "tool.call":
        parts = [f"tool.call {attrs.get('tool', '?')}  {dur}", status]
        if status == "denied":
            parts.append(f"by {attrs.get('denied_by', '?')}")
        if attrs.get("exec_ms") is not None:
            parts.append(f"ran {fmt_ms(attrs['exec_ms'])}")
        if attrs.get("risk"):
            parts.append(f"risk {attrs['risk']}")
    elif name == "approval.wait":
        parts = [f"approval.wait {attrs.get('tool', '?')}  {dur}", str(attrs.get("outcome", status))]
    elif name == "compaction":
        parts = [f"compaction {attrs.get('trigger', '?')}  {dur}", "compacted" if attrs.get("compacted") else "did not compact"]
    else:
        parts = [f"{name}  {dur}", status]
    line = "  ".join(parts)
    if status == "error" and attrs.get("error"):
        line += f"  ! {str(attrs['error'])[:100]}"
    return line


def format_tree(spans: list[Span]) -> list[str]:
    """The spans of one trace as an indented tree, children in the order they started."""
    ids = {s["span"] for s in spans}
    children: dict[str, list[Span]] = defaultdict(list)
    roots: list[Span] = []
    for span in sorted(spans, key=lambda s: s["start"]):
        parent = span.get("parent")
        (children[parent] if parent in ids else roots).append(span)
    lines: list[str] = []

    def walk(span: Span, prefix: str, last: bool, top: bool) -> None:
        lines.append(_describe(span) if top else prefix + ("└─ " if last else "├─ ") + _describe(span))
        child_prefix = "" if top else prefix + ("   " if last else "│  ")
        kids = children.get(span["span"], [])
        for index, kid in enumerate(kids):
            walk(kid, child_prefix, index == len(kids) - 1, False)

    for root in roots:
        walk(root, "", True, True)
    return lines


def _union_ms(spans: list[Span]) -> float:
    """Wall-clock time covered by spans that may overlap (tool calls running side by side)."""
    intervals = sorted((_epoch(s), _epoch(s) + s["ms"] / 1000) for s in spans)
    total, end = 0.0, None
    for start, stop in intervals:
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total * 1000


@dataclass
class RunSummary:
    """Where the wall-clock time of one run went. The five parts add up to ``total_ms``."""

    trace: str
    total_ms: float
    model_ms: float
    tools_ms: float
    approval_ms: float
    compaction_ms: float
    other_ms: float
    model_calls: int
    tool_calls: int
    denied: int
    tokens: int
    cost_usd: float
    unpriced: bool
    slowest: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def summarize(spans: list[Span], trace: str) -> RunSummary | None:
    """Split one run's time into model calls, tools, approval waits, compaction and the rest."""
    mine = [s for s in spans if s["trace"] == trace]
    run = next((s for s in mine if s["name"] == "run"), None)
    if run is None:
        return None
    by_id = {s["span"]: s for s in mine}

    def parent_name(span: Span) -> str | None:
        parent = by_id.get(span.get("parent"))
        return parent["name"] if parent is not None else None

    model_all = [s for s in mine if s["name"] == "model.call"]
    model = [s for s in model_all if parent_name(s) != "compaction"]  # those belong to the compaction
    compaction = [s for s in mine if s["name"] == "compaction"]
    tools = [s for s in mine if s["name"] == "tool.call"]
    approvals = [s for s in mine if s["name"] == "approval.wait"]

    model_ms = sum(s["ms"] for s in model)
    compaction_ms = sum(s["ms"] for s in compaction)
    approval_ms = _union_ms(approvals)
    tools_ms = max(0.0, _union_ms(tools) - approval_ms)  # an approval wait happens inside its tool call
    other_ms = max(0.0, run["ms"] - model_ms - compaction_ms - approval_ms - tools_ms)

    priced = [_attrs(s).get("cost_usd") for s in model_all]

    def where(span: Span) -> str:
        turn = by_id.get(span.get("parent"))
        label = span["name"] + (f" {_attrs(span).get('tool')}" if span["name"] == "tool.call" else "")
        at = f" (turn {_attrs(turn).get('index')})" if turn is not None and turn["name"] == "turn" else ""
        return f"{label} {fmt_ms(span['ms'])}{at}"

    slowest = [where(s) for s in sorted(model + tools + compaction, key=lambda s: s["ms"], reverse=True)[:3]]
    problems = [
        f"{where(s)}: {_attrs(s).get('error') or 'failed'}"
        for s in mine
        if s.get("status") == "error" and s["name"] not in ("run", "turn")
    ]
    return RunSummary(
        trace=trace,
        total_ms=run["ms"],
        model_ms=model_ms,
        tools_ms=tools_ms,
        approval_ms=approval_ms,
        compaction_ms=compaction_ms,
        other_ms=other_ms,
        model_calls=len(model_all),
        tool_calls=len(tools),
        denied=sum(1 for s in tools if s.get("status") == "denied"),
        tokens=sum((_attrs(s).get("input_tokens") or 0) + (_attrs(s).get("output_tokens") or 0) for s in model_all),
        cost_usd=sum(c for c in priced if c is not None),
        unpriced=any(c is None for c in priced),
        slowest=slowest,
        problems=problems,
    )


def format_summary(summary: RunSummary) -> list[str]:
    total = summary.total_ms or 1.0

    def row(label: str, ms: float, note: str = "") -> str:
        return f"  {label:<14}{fmt_ms(ms):>8}  {ms / total * 100:>3.0f}%  {note}".rstrip()

    cost = "unpriced" if summary.unpriced and not summary.cost_usd else _cost(summary.cost_usd)
    lines = [
        f"where the time went ({fmt_ms(summary.total_ms)}):",
        row("model calls", summary.model_ms, f"{summary.model_calls} calls, {summary.tokens:,} tokens, {cost}"),
        row("tools", summary.tools_ms, f"{summary.tool_calls} calls" + (f", {summary.denied} denied" if summary.denied else "")),
        row("approval wait", summary.approval_ms),
        row("compaction", summary.compaction_ms),
        row("other", summary.other_ms),
    ]
    if summary.slowest:
        lines.append("slowest: " + ", ".join(summary.slowest))
    for problem in summary.problems:
        lines.append(f"problem: {problem}")
    return lines


def format_session(path: Path, *, run: int | None = None, all_runs: bool = False) -> str:
    """The tree and time breakdown of a session's last run (or of run number ``run``, or of all)."""
    spans = read_spans(path)
    found = runs(spans)
    if not found:
        return f"No finished run in {path}"
    if run is not None and not 1 <= run <= len(found):
        return f"This trace has {len(found)} run(s); there is no run {run}"
    chosen = list(enumerate(found, 1)) if all_runs else [(run, found[run - 1]) if run else (len(found), found[-1])]
    blocks: list[str] = []
    for number, root in chosen:
        block = [f"run {number} of {len(found)}  trace {root['trace'][:8]}  started {root['start']}", ""]
        block += format_tree([s for s in spans if s["trace"] == root["trace"]])
        summary = summarize(spans, root["trace"])
        if summary is not None:
            block += ["", *format_summary(summary)]
        blocks.append("\n".join(block))
    run_traces = {r["trace"] for r in found}
    outside = [s for s in spans if s["trace"] not in run_traces and s["name"] == "model.call"]
    text = "\n\n".join(blocks)
    if outside:
        tokens = sum((_attrs(s).get("input_tokens") or 0) + (_attrs(s).get("output_tokens") or 0) for s in outside)
        cost = sum(c for c in (_attrs(s).get("cost_usd") for s in outside) if c is not None)
        text += (
            f"\n\noutside any run: {len(outside)} model calls (for example memory), "
            f"{fmt_ms(sum(s['ms'] for s in outside))}, {tokens:,} tokens, {_cost(cost)}"
        )
    return text


@dataclass
class SessionRow:
    session: str
    started: str
    runs: int
    last_stop: str
    total_ms: float
    cost_usd: float


def overview(path: Path) -> SessionRow | None:
    """One line of facts about a trace file, or None when it holds no finished run."""
    spans = read_spans(path)
    found = runs(spans)
    if not found:
        return None
    return SessionRow(
        session=str(found[0].get("session") or path.stem),
        started=found[0]["start"],
        runs=len(found),
        last_stop=str(_attrs(found[-1]).get("stop_reason", found[-1].get("status", "?"))),
        total_ms=sum(r["ms"] for r in found),
        cost_usd=sum(_attrs(r).get("cost_usd") or 0.0 for r in found),
    )
