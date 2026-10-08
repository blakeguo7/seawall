"""Where trace files live and how to find them."""

from __future__ import annotations

from pathlib import Path

from seawall.config.paths import get_data_dir
from seawall.config.settings import TraceSettings
from seawall.tracing.tracer import NULL_TRACER, AnyTracer, Tracer


def trace_dir(settings: TraceSettings) -> Path:
    """Directory holding the session trace files."""
    if settings.directory:
        return Path(settings.directory).expanduser()
    return get_data_dir() / "traces"


def open_session_trace(settings: TraceSettings, session_id: str) -> AnyTracer:
    """The tracer for a session, or a no-op one when tracing is off. Nothing is written until a span ends."""
    if not settings.enabled:
        return NULL_TRACER
    return Tracer(
        trace_dir(settings) / f"{session_id}.jsonl",
        session_id=session_id,
        capture_content=settings.capture_content,
        max_field_chars=settings.max_field_chars,
    )


def list_traces(directory: Path) -> list[Path]:
    """Session trace files in ``directory``, newest first."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def resolve_trace(directory: Path, ref: str) -> Path | None:
    """Find a trace file by path, full session id or unambiguous session-id prefix."""
    candidate = Path(ref).expanduser()
    if candidate.is_file():
        return candidate
    matches = [path for path in list_traces(directory) if path.stem.startswith(ref)]
    return matches[0] if len(matches) == 1 else None
