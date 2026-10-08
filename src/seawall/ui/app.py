"""Interactive session entry points."""

from __future__ import annotations

import asyncio
import json
import sys

from seawall.coordinator.coordinator_mode import is_coordinator_mode

from seawall.api.client import SupportsStreamingMessages
from seawall.audit import summarize_tool_input
from seawall.engine.stream_events import StreamEvent
from seawall.permissions.approvals import DenyApprover
from seawall.ui.backend_host import run_backend_host
from seawall.ui.coordinator_drain import drain_coordinator_async_agents
from seawall.ui.react_launcher import launch_react_tui
from seawall.ui.overrides import SessionOverrides
from seawall.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime


def _decode_task_worker_line(raw: str) -> str:
    """Normalize one stdin line for the headless task worker.

    Task-manager driven agent workers receive either:
    - a plain text line (initial prompt or simple follow-up), or
    - a JSON object from ``send_message`` / teammate backends with a ``text`` field.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
    return stripped


async def run_repl(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    backend_only: bool = False,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    permission_mode: str | None = None,
    overrides: SessionOverrides | None = None,
) -> None:
    """Run the default Seawall interactive application (React TUI)."""
    if backend_only:
        await run_backend_host(
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            effort=effort,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            api_client=api_client,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=max_turns is not None,
            permission_mode=permission_mode,
            overrides=overrides,
        )
        return

    exit_code = await launch_react_tui(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        permission_mode=permission_mode,
        overrides=overrides,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


async def run_task_worker(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
    overrides: SessionOverrides | None = None,
) -> None:
    """Run a stdin-driven headless worker for background agent tasks.

    This mode exists for subprocess teammates and other task-manager managed
    agent processes. It intentionally avoids the React TUI / Ink path so it
    can run without a controlling TTY.

    A worker cannot ask anyone for approval. It runs under the permission mode it
    was started with, and a call that would need confirmation is refused. It does
    not get to approve its own requests.
    """

    async def _noop_ask(_question: str) -> str:
        return ""

    async def _print_system(message: str) -> None:
        print(message, flush=True)

    async def _render_event(event: StreamEvent) -> None:
        from seawall.engine.stream_events import AssistantTextDelta, AssistantTurnComplete, ErrorEvent, StatusEvent

        if isinstance(event, AssistantTextDelta):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, AssistantTurnComplete):
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif isinstance(event, ErrorEvent):
            print(event.message, flush=True)
        elif isinstance(event, StatusEvent) and event.message:
            print(event.message, flush=True)

    async def _clear_output() -> None:
        return None

    bundle = await build_runtime(
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        api_client=api_client,
        approver=DenyApprover(),
        ask_user_prompt=_noop_ask,
        enforce_max_turns=max_turns is not None,
        permission_mode=permission_mode,
        overrides=overrides,
    )
    await start_runtime(bundle)
    try:
        while True:
            raw = await asyncio.to_thread(sys.stdin.readline)
            if raw == "":
                break
            line = _decode_task_worker_line(raw)
            if not line:
                continue
            await handle_line(
                bundle,
                line,
                print_system=_print_system,
                render_event=_render_event,
                clear_output=_clear_output,
            )
            # Background agent tasks are one-shot workers. If the coordinator
            # needs to send a follow-up later, BackgroundTaskManager already
            # knows how to restart the task and write the next stdin payload.
            break
    finally:
        await close_runtime(bundle)


async def run_print_mode(
    *,
    prompt: str,
    output_format: str = "text",
    cwd: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
    max_turns: int | None = None,
    overrides: SessionOverrides | None = None,
) -> int:
    """Non-interactive mode: submit prompt, stream output, return the exit code.

    The exit code is 0 when the model finished on its own, 1 after an API error, and 2 when
    the run was cut short (turn, budget, token, time or loop limit). The ``json`` result and the
    last ``stream-json`` event say which, and what the run cost.

    Nobody is there to answer an approval prompt, so a call that needs one is
    refused, not approved. To let a headless run act, choose a mode that does not
    ask (``--permission-mode full_auto``) or list the tools it may use
    (``--allowed-tools``). Refused calls are reported: on stderr for ``text``, as
    ``permission_denials`` in the ``json`` result and as ``denied`` on the
    ``tool_completed`` events of ``stream-json``.
    """
    from collections import deque

    from seawall.engine.stream_events import (
        AssistantTextDelta,
        AssistantTurnComplete,
        CompactProgressEvent,
        ErrorEvent,
        StatusEvent,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

    async def _noop_ask(question: str) -> str:
        return ""

    bundle = await build_runtime(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        enforce_max_turns=True,
        api_client=api_client,
        approver=DenyApprover(),
        ask_user_prompt=_noop_ask,
        permission_mode=permission_mode,
        overrides=overrides,
    )
    await start_runtime(bundle)

    collected_text = ""
    events_list: list[dict] = []
    permission_denials: list[dict] = []
    started_calls: deque[tuple[str, dict]] = deque()

    try:
        async def _print_system(message: str) -> None:
            nonlocal collected_text
            if output_format == "text":
                print(message, file=sys.stderr)
            elif output_format == "stream-json":
                obj = {"type": "system", "message": message}
                print(json.dumps(obj), flush=True)
                events_list.append(obj)

        async def _render_event(event: StreamEvent) -> None:
            nonlocal collected_text
            if isinstance(event, AssistantTextDelta):
                collected_text += event.text
                if output_format == "text":
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif output_format == "stream-json":
                    obj = {"type": "assistant_delta", "text": event.text}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, AssistantTurnComplete):
                if output_format == "text":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif output_format == "stream-json":
                    obj = {"type": "assistant_complete", "text": event.message.text.strip()}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionStarted):
                started_calls.append((event.tool_name, dict(event.tool_input)))
                if output_format == "stream-json":
                    obj = {"type": "tool_started", "tool_name": event.tool_name, "tool_input": event.tool_input}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionCompleted):
                tool_input = started_calls.popleft()[1] if started_calls else {}
                denied = bool((event.metadata or {}).get("denied"))
                if denied:
                    permission_denials.append(
                        {
                            "tool_name": event.tool_name,
                            "tool_input": summarize_tool_input(tool_input),
                            "denied_by": (event.metadata or {}).get("denied_by"),
                            "message": event.output,
                        }
                    )
                    if output_format == "text":
                        print(f"[not run] {event.tool_name}: {event.output.splitlines()[0]}", file=sys.stderr)
                if output_format == "stream-json":
                    obj = {
                        "type": "tool_completed",
                        "tool_name": event.tool_name,
                        "output": event.output,
                        "is_error": event.is_error,
                        "denied": denied,
                    }
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ErrorEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "error", "message": event.message, "recoverable": event.recoverable}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, CompactProgressEvent):
                if output_format == "text" and event.message:
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {
                        "type": "compact_progress",
                        "phase": event.phase,
                        "trigger": event.trigger,
                        "attempt": event.attempt,
                        "message": event.message,
                    }
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, StatusEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "status", "message": event.message}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)

        async def _clear_output() -> None:
            pass

        await handle_line(
            bundle,
            prompt,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )
        if is_coordinator_mode():
            await drain_coordinator_async_agents(
                bundle,
                prompt_seed=prompt,
                print_system=_print_system,
                render_event=_render_event,
                announce_waiting=output_format == "text",
            )

        summary = _run_summary(bundle.engine)
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "type": "result",
                        "text": collected_text.strip(),
                        **summary,
                        "permission_denials": permission_denials,
                    }
                )
            )
        elif output_format == "stream-json":
            print(json.dumps({"type": "run_finished", **summary}), flush=True)
        elif summary["stop_reason"] not in {"completed", "error"}:
            print(f"[stopped: {summary['stop_reason']}] {summary['detail']}", file=sys.stderr)
        return _EXIT_CODES.get(summary["stop_reason"], 2)
    finally:
        await close_runtime(bundle)


_EXIT_CODES = {"completed": 0, "error": 1}


def _run_summary(engine) -> dict:
    """How the last run ended and what the session has used, as plain JSON values."""
    tracker = engine.cost_tracker
    usage = tracker.total
    last = engine.last_run
    return {
        "stop_reason": last.stop_reason if last is not None else "completed",
        "detail": last.detail if last is not None else "",
        "turns": last.turns if last is not None else 0,
        "duration_seconds": round(last.duration_seconds, 3) if last is not None else 0.0,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        },
        "cost_usd": None if tracker.unpriced_models else round(tracker.cost_usd, 6),
        "unpriced_models": list(tracker.unpriced_models),
    }
