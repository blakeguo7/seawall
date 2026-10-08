"""Audit trail for tool calls, approvals and session lifecycle."""

from seawall.audit.log import (
    NULL_AUDIT,
    AuditLog,
    AuditSink,
    AuditWriteError,
    NullAudit,
    VerifyResult,
    key_from_env,
    read_records,
    verify_log,
)
from seawall.audit.redact import (
    describe_tool_call,
    digest,
    redact_text,
    summarize_tool_input,
    truncate,
)
from seawall.audit.store import audit_dir, list_logs, open_session_audit, resolve_log

__all__ = [
    "NULL_AUDIT",
    "AuditLog",
    "AuditSink",
    "AuditWriteError",
    "NullAudit",
    "VerifyResult",
    "audit_dir",
    "describe_tool_call",
    "digest",
    "key_from_env",
    "list_logs",
    "open_session_audit",
    "read_records",
    "redact_text",
    "resolve_log",
    "summarize_tool_input",
    "truncate",
    "verify_log",
]
