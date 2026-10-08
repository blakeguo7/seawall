"""Keep secrets and bulk content out of audit records and approval prompts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

REDACTED = "[REDACTED]"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)
_TOKEN_FORMATS = re.compile(
    r"""
    sk-ant-[A-Za-z0-9_\-]{16,}
    | sk-[A-Za-z0-9_\-]{20,}
    | A(?:KIA|SIA)[0-9A-Z]{16}
    | gh[pousr]_[A-Za-z0-9]{30,}
    | github_pat_[A-Za-z0-9_]{30,}
    | glpat-[A-Za-z0-9_\-]{20,}
    | xox[abprs]-[A-Za-z0-9\-]{10,}
    | AIza[0-9A-Za-z_\-]{30,}
    | npm_[A-Za-z0-9]{30,}
    | pypi-[A-Za-z0-9_\-]{30,}
    | hf_[A-Za-z0-9]{30,}
    | eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}
    """,
    re.VERBOSE,
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{16,}=*")
_URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+:)[^/\s@]+(@)")
_SECRET_FLAG = re.compile(
    r"""(?ix)(--?(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|password|passwd|secret)[=\s]+)
    ("[^"]*"|'[^']*'|[^\s"']+)"""
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)\b([\w.\-]*(?:api[_-]?key|secret|token|passw(?:or)?d|credential|auth)[\w.\-]*)
    (["']?\s*[=:]\s*)(?!\[REDACTED)("[^"]*"|'[^']*'|[^\s"';&|]+)"""
)

# Inputs that carry file contents or large blobs: logged as a length and a digest only.
_CONTENT_KEYS = frozenset(
    {"content", "new_str", "old_str", "new_source", "old_source", "text", "body", "data",
     "file_text", "patch", "diff"}
)
_PATH_KEYS = ("command", "file_path", "path", "root", "url", "query", "pattern", "prompt")


def redact_text(text: str) -> str:
    """Replace credentials in ``text`` with ``[REDACTED]``."""
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _TOKEN_FORMATS.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _URL_CREDENTIALS.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    text = _SECRET_FLAG.sub(lambda m: m.group(1) + REDACTED, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…[+{len(text) - limit} chars]"


def digest(text: str) -> str:
    """Short, stable fingerprint of ``text`` for correlating records."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def summarize_tool_input(
    tool_input: Mapping[str, object], *, max_chars: int = 1000
) -> dict[str, Any]:
    """Return a JSON-safe, redacted copy of a tool's arguments for the audit log.

    File contents and other large payloads are reduced to their length and a
    digest, so the log shows *what* was written without becoming a second copy
    of the workspace (or of any secret in it).
    """
    summary: dict[str, Any] = {}
    for key, value in tool_input.items():
        if isinstance(value, str):
            if key in _CONTENT_KEYS:
                summary[key] = {"chars": len(value), "sha256": digest(value)}
            else:
                summary[key] = truncate(redact_text(value), max_chars)
        elif value is None or isinstance(value, (bool, int, float)):
            summary[key] = value
        else:
            rendered = json.dumps(value, default=str, ensure_ascii=False)
            summary[key] = truncate(redact_text(rendered), max_chars)
    return summary


def describe_tool_call(tool_input: Mapping[str, object], *, max_chars: int = 400) -> str:
    """One redacted line saying what a tool call is about to do, for approval prompts."""
    parts: list[str] = []
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value if key == "command" else f"{key}={value}")
            if key == "command":
                break
            if len(parts) == 2:
                break
    if not parts:
        parts.append(json.dumps(summarize_tool_input(tool_input, max_chars=80), ensure_ascii=False))
    line = " ".join(parts).replace("\n", " ⏎ ")
    return truncate(redact_text(line), max_chars)
