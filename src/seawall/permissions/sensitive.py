"""Credential locations that no permission mode or setting can unlock."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable

# Paths that are always denied regardless of permission mode or user config.
# These protect high-value credential and key material from LLM-directed access
# (including via prompt injection).  Patterns use fnmatch syntax and are matched
# against the fully-resolved absolute path produced by the query engine.
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # SSH keys and config
    "*/.ssh/*",
    # AWS credentials
    "*/.aws/credentials",
    "*/.aws/config",
    # GCP credentials
    "*/.config/gcloud/*",
    # Azure credentials
    "*/.azure/*",
    # GPG keys
    "*/.gnupg/*",
    # Docker credentials
    "*/.docker/config.json",
    # Kubernetes credentials
    "*/.kube/config",
    # Seawall's own credential stores
    "*/.seawall/credentials.json",
    # Other well-known credential stores
    "*/.netrc",
    "*/.pgpass",
    "*/.git-credentials",
    "*/.pypirc",
    "*/.config/gh/hosts.yml",
    "*/.password-store/*",
    "*/Library/Keychains/*",
    "*/.local/share/keyrings/*",
    # System account databases
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/*",
)


def policy_match_paths(file_path: str) -> tuple[str, ...]:
    """Return path forms that should participate in policy matching.

    Directory-scoped tools like ``grep`` and ``glob`` may operate on a root such
    as ``/home/user/.ssh``. Appending a trailing slash lets glob-style deny
    patterns like ``*/.ssh/*`` and ``/etc/*`` match the directory root itself.
    """
    normalized = file_path.rstrip("/")
    if not normalized:
        return (file_path,)
    return (normalized, normalized + "/")


def match_sensitive_path(file_path: str) -> str | None:
    """Return the built-in pattern that ``file_path`` matches, if any."""
    for candidate_path in policy_match_paths(file_path):
        for pattern in SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(candidate_path, pattern):
                return pattern
    return None


def is_sensitive_path(path: str | os.PathLike[str]) -> bool:
    """True if ``path`` is a credential location that must never be read or listed."""
    return match_sensitive_path(os.fspath(path)) is not None


def drop_sensitive(root: Path, relative_paths: Iterable[str]) -> list[str]:
    """Remove credential files from a list of paths found under ``root``.

    The permission checker only sees the root a directory-scoped tool (grep, glob) was pointed
    at, so ``grep -r KEY ~`` would walk straight through ``~/.ssh``. The tools call this on what
    they found, whichever search backend produced it.
    """
    return [
        relative
        for relative in relative_paths
        if not is_sensitive_path(os.path.normpath(os.path.join(root, relative)))
    ]
