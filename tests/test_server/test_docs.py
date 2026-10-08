"""The examples in docs/server.md are run, so they cannot drift from the server."""

from __future__ import annotations

import re
from pathlib import Path

from tests.fakes import ScriptedClient, text_message, tool_message
from tests.test_server.conftest import TOKEN, plain_help

DOC = Path(__file__).resolve().parents[2] / "docs" / "server.md"


def python_example() -> str:
    section = DOC.read_text().split("## A Python client", 1)[1]
    return re.search(r"```python\n(.*?)```", section, re.S).group(1)


def test_the_python_client_example_works_against_a_running_server(live, provider, capsys, workspace):
    code = python_example()
    code = re.sub(r'BASE, TOKEN = "[^"]*", "[^"]*"', f'BASE, TOKEN = "{live.url}", "{TOKEN}"', code)
    assert live.url in code

    # the example creates a session itself; script the model for whichever session that will be
    original = provider.default
    provider.default = lambda: ScriptedClient(
        tool_message(("write_file", {"path": "x.txt", "content": "x"})),  # the example refuses approvals
        text_message("I could not write it."),
    )
    try:
        exec(compile(code, "docs/server.md", "exec"), {})
    finally:
        provider.default = original

    printed = capsys.readouterr().out
    assert "approve write_file" in printed  # it saw the approval request and answered it
    assert "I could not write it." in printed  # it streamed the answer
    assert "completed" in printed  # and the end of the run
    assert not (workspace / "x.txt").exists()


def test_the_documented_options_exist():
    """Every ``--flag`` in the options table is a real option of ``seawall serve``."""
    table = DOC.read_text().split("## Starting the server", 1)[1].split("## Authentication", 1)[0]
    documented = set(re.findall(r"`(--[a-z-]+)`", table))
    help_text = plain_help("serve")
    assert documented and all(flag in help_text for flag in documented), documented - set(re.findall(r"--[a-z-]+", help_text))
    # and every option of the command is documented
    real = set(re.findall(r"--[a-z][a-z-]+", help_text)) - {"--help"}
    assert real <= documented, real - documented


def test_the_documented_status_codes_and_error_codes_exist():
    """The error codes named in the text are ones the server can actually send."""
    sources = "".join(p.read_text() for p in (DOC.parents[1] / "src" / "seawall" / "server").glob("*.py"))
    for code in re.findall(r"`(?:\d{3} )?([a-z_]+)`", DOC.read_text()):
        if code in {"session_elsewhere", "busy", "overloaded", "unattended_disabled", "limit_too_high",
                    "already_resolved", "not_pending_here"}:  # fmt: skip
            assert f'"{code}"' in sources, code
