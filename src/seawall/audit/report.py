"""Turn audit records into something a person can read."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from seawall.audit.log import read_records, verify_log
from seawall.audit.redact import truncate
from seawall.permissions.risk import RiskLevel


@dataclass(frozen=True)
class LogSummary:
    """One line of ``seawall audit list``."""

    path: Path
    session_id: str
    started: str
    cwd: str
    mode: str
    records: int
    denied: int
    verified: bool
    closed: bool
    error: str | None


def summarize_log(path: Path, *, key: bytes | None = None) -> LogSummary:
    """Read a log's headline facts and check its chain."""
    records = list(read_records(path))
    first = records[0] if records else {}
    data = first.get("data") if isinstance(first.get("data"), dict) else {}
    result = verify_log(path, key=key)
    denied = sum(
        1
        for record in records
        if record.get("type") == "tool.decision" and record.get("data", {}).get("decision") == "deny"
    )
    return LogSummary(
        path=path,
        session_id=str(first.get("session") or path.stem),
        started=str(first.get("ts") or "")[:19].replace("T", " "),
        cwd=str(data.get("cwd") or ""),
        mode=str(data.get("permission_mode") or ""),
        records=len(records),
        denied=denied,
        verified=result.ok,
        closed=result.closed,
        error=result.error,
    )


def filter_records(
    records: Iterable[dict[str, Any]],
    *,
    tool: str | None = None,
    decision: str | None = None,
    min_risk: RiskLevel | None = None,
) -> Iterator[dict[str, Any]]:
    """Keep the tool records that match every given filter; other records are kept only
    when no filter is set."""
    filtering = tool is not None or decision is not None or min_risk is not None
    for record in records:
        if not filtering:
            yield record
            continue
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if record.get("type") != "tool.decision":
            continue
        if tool is not None and str(data.get("tool", "")).lower() != tool.lower():
            continue
        if decision is not None and data.get("decision") != decision:
            continue
        if min_risk is not None:
            try:
                level = RiskLevel.parse(str(data.get("risk", "low")))
            except ValueError:
                continue
            if level < min_risk:
                continue
        yield record


def _what(data: dict[str, Any]) -> str:
    """The most telling field of a tool call's logged input."""
    tool_input = data.get("input")
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "path", "file_path", "url", "pattern", "query", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return truncate(value.replace("\n", " ⏎ "), 100)
    return ""


def format_record(record: dict[str, Any]) -> str:
    """One line per record: time, what happened, and the details that matter."""
    kind = str(record.get("type", "?"))
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    stamp = str(record.get("ts", ""))[11:23]
    head = f"{record.get('seq', '?'):>4}  {stamp}  "
    if kind == "tool.decision":
        verdict = str(data.get("decision", "?")).upper()
        risk = str(data.get("risk", ""))
        source = str(data.get("source", ""))
        approval = data.get("approval")
        by = f" by {approval.get('decided_by')}" if isinstance(approval, dict) else ""
        line = f"{verdict:<5} {data.get('tool', '?')} [{risk}] {_what(data)}  ({source}{by})"
        if verdict == "DENY" and data.get("reason"):
            line += f"\n{'':>20}{truncate(str(data['reason']), 240)}"
        return head + line
    if kind == "tool.result":
        return head + f"      {data.get('tool', '?')} {data.get('status')} in {data.get('duration_ms')}ms"
    if kind == "approval.requested":
        return head + f"ASK   {data.get('tool', '?')} [{data.get('risk', '')}] {truncate(str(data.get('summary', '')), 100)}"
    if kind == "approval.resolved":
        return head + (
            f"      answer: {data.get('outcome')} by {data.get('decided_by')} "
            f"after {data.get('wait_ms')}ms"
        )
    if kind == "session.start":
        return head + f"session started  cwd={data.get('cwd')}  mode={data.get('permission_mode')}  model={data.get('model')}"
    if kind == "session.end":
        return head + "session ended"
    detail = " ".join(f"{k}={v}" for k, v in data.items() if k not in {"chain", "format"})
    return head + f"{kind} {truncate(detail, 120)}"
