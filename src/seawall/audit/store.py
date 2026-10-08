"""Where audit logs live and how to find them."""

from __future__ import annotations

import logging
from pathlib import Path

from seawall.audit.log import NULL_AUDIT, AuditLog, AuditSink, AuditWriteError, key_from_env
from seawall.config.paths import get_data_dir
from seawall.config.settings import AuditSettings

log = logging.getLogger(__name__)


def audit_dir(settings: AuditSettings) -> Path:
    """Directory holding the session logs."""
    if settings.directory:
        return Path(settings.directory).expanduser()
    return get_data_dir() / "audit"


def open_session_audit(settings: AuditSettings, session_id: str) -> AuditSink:
    """Create the audit log for a session, or a no-op sink when auditing is off.

    A log that cannot be created is a warning, not a crash, unless ``fail_closed``
    is set: then the session must not start without its audit trail.
    """
    if not settings.enabled:
        return NULL_AUDIT
    path = audit_dir(settings) / f"{session_id}.jsonl"
    try:
        return AuditLog(
            path,
            session_id=session_id,
            key=key_from_env(settings.key_env),
            fail_closed=settings.fail_closed,
        )
    except OSError as exc:
        if settings.fail_closed:
            raise AuditWriteError(f"cannot create audit log {path}: {exc}") from exc
        log.warning("audit log %s cannot be created, continuing without it: %s", path, exc)
        return NULL_AUDIT


def list_logs(directory: Path) -> list[Path]:
    """Session logs in ``directory``, newest first."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def resolve_log(directory: Path, ref: str) -> Path | None:
    """Find a log by file path, full session id or unambiguous session-id prefix."""
    candidate = Path(ref).expanduser()
    if candidate.is_file():
        return candidate
    matches = [path for path in list_logs(directory) if path.stem.startswith(ref)]
    return matches[0] if len(matches) == 1 else None
