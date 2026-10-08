"""Tests for redaction of secrets in audit records and approval prompts."""

from __future__ import annotations

import pytest

from seawall.audit import describe_tool_call, redact_text, summarize_tool_input, truncate
from seawall.audit.redact import REDACTED


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-abcdefghijklmnopqrstuvwxyz0123",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ],
)
def test_token_formats_are_redacted(secret: str) -> None:
    text = f"curl -H 'x: {secret}' https://example.com"
    assert secret not in redact_text(text)
    assert REDACTED in redact_text(text)


def test_private_keys_are_redacted() -> None:
    text = "echo '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----' > k"
    redacted = redact_text(text)
    assert "MIIEow" not in redacted
    assert "PRIVATE KEY" in redacted


def test_bearer_and_url_credentials_are_redacted() -> None:
    assert "abcdefghijklmnop1234" not in redact_text("Authorization: Bearer abcdefghijklmnop1234")
    redacted = redact_text("git clone https://user:hunter2@example.com/repo.git")
    assert "hunter2" not in redacted
    assert "https://user:" in redacted and "@example.com" in redacted


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("export API_KEY=abc123xyz", "API_KEY="),
        ('DB_PASSWORD="p@ss word"', "DB_PASSWORD="),
        ("curl --password hunter2 https://x", "--password "),
        ("mytool --token=abcdef", "--token="),
        ("auth_token: s3cr3t", "auth_token"),
    ],
)
def test_assignments_keep_the_name_and_hide_the_value(text: str, kept: str) -> None:
    redacted = redact_text(text)
    assert kept in redacted
    for value in ("abc123xyz", "p@ss word", "hunter2", "abcdef", "s3cr3t"):
        assert value not in redacted


@pytest.mark.parametrize(
    "text", ["ls -la", "git commit -m 'fix the author field'", "pytest tests/test_auth.py", "echo hello world"]
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert redact_text(text) == text


def test_truncate() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 3) == "abc…[+3 chars]"


def test_file_contents_are_logged_as_a_digest_not_a_copy() -> None:
    secret_file = "API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123\n" * 50
    summary = summarize_tool_input({"path": "/work/.env", "content": secret_file, "append": True})

    assert summary["path"] == "/work/.env"
    assert summary["append"] is True
    assert summary["content"]["chars"] == len(secret_file)
    assert len(summary["content"]["sha256"]) == 16
    assert "sk-abc" not in str(summary)


def test_long_strings_and_structures_are_truncated_and_redacted() -> None:
    summary = summarize_tool_input(
        {"command": "echo " + "x" * 5000, "env": {"TOKEN": "tok_abcdefghijklmnop"}, "n": 3},
        max_chars=100,
    )
    assert len(summary["command"]) < 200
    assert summary["command"].endswith("chars]")
    assert "tok_abcdefghijklmnop" not in summary["env"]
    assert summary["n"] == 3


def test_describe_tool_call_prefers_the_command() -> None:
    assert describe_tool_call({"command": "rm -rf build", "cwd": "/x"}) == "rm -rf build"


def test_describe_tool_call_shows_path_like_fields() -> None:
    line = describe_tool_call({"path": "src/app.py", "old_str": "a", "new_str": "b"})
    assert line == "path=src/app.py"
    assert describe_tool_call({"url": "https://example.com/a", "prompt": "summarise"}) == (
        "url=https://example.com/a prompt=summarise"
    )


def test_describe_tool_call_flattens_newlines_and_redacts() -> None:
    line = describe_tool_call({"command": "export TOKEN=abc123xyz\nrm -rf build"})
    assert "\n" not in line
    assert "abc123xyz" not in line
    assert "rm -rf build" in line


def test_describe_tool_call_falls_back_to_a_summary() -> None:
    assert describe_tool_call({"items": [1, 2, 3]}) == '{"items": "[1, 2, 3]"}'
