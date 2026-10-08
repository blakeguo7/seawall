"""Approvals and audit around tool calls in the agent loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from seawall.config.settings import PermissionSettings
from seawall.engine.query import QueryContext, _execute_tool_call
from seawall.engine.query_engine import QueryEngine
from seawall.engine.stream_events import ToolExecutionCompleted
from seawall.hooks.types import AggregatedHookResult, HookResult
from seawall.permissions import PermissionChecker, PermissionMode
from seawall.permissions.approvals import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalOutcome,
    AutoApprover,
    CallbackApprover,
)
from seawall.tools import create_default_tool_registry
from tests.fakes import RecordingAudit, ScriptedClient, text_message, tool_message


def make_context(tmp_path: Path, *, mode=PermissionMode.DEFAULT, approver=None, audit=None, hooks=None, **settings):
    return QueryContext(
        api_client=ScriptedClient(),
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=mode, **settings)),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        max_tokens=100,
        max_turns=2,
        approver=approver,
        audit=audit or RecordingAudit(),
        hook_executor=hooks,
        tool_metadata={"session_id": "sess-1"},
    )


async def run_bash(context: QueryContext, command: str):
    return await _execute_tool_call(context, "bash", "toolu_1", {"command": command})


# --- nobody to ask -----------------------------------------------------------------------


async def test_a_call_that_needs_approval_is_refused_when_nobody_can_ask(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, audit=audit)  # no approver, as in a headless run

    result = await run_bash(context, "mkdir made-anyway")

    assert result.is_error is True
    assert not (tmp_path / "made-anyway").exists()
    assert result.result_metadata == {"denied": True, "denied_by": "approval", "risk": "low"}
    assert "cannot ask anyone" in result.content
    assert "--permission-mode full_auto" in result.content
    assert "--allowed-tools bash" in result.content

    assert audit.types() == ["approval.requested", "approval.resolved", "tool.decision"]
    decision = audit.of("tool.decision")[0]
    assert decision["decision"] == "deny"
    assert decision["source"] == "approval"
    assert decision["approval"]["outcome"] == "unavailable"
    assert audit.of("approval.resolved")[0]["outcome"] == "unavailable"


# --- approved and denied ------------------------------------------------------------------


async def test_an_approved_call_runs_and_the_trail_shows_who_approved(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, approver=AutoApprover("test-user"), audit=audit)

    result = await run_bash(context, "mkdir made")

    assert result.is_error is False
    assert (tmp_path / "made").is_dir()
    assert audit.types() == ["approval.requested", "approval.resolved", "tool.decision", "tool.result"]
    decision = audit.of("tool.decision")[0]
    assert decision["decision"] == "allow"
    assert decision["source"] == "approval"
    assert decision["approval"]["decided_by"] == "test-user"
    assert decision["mode"] == "default"
    assert decision["input"] == {"command": "mkdir made"}
    outcome = audit.of("tool.result")[0]
    assert outcome["status"] == "ok"
    assert outcome["call_id"] == decision["call_id"] == "toolu_1"
    assert len(outcome["output_sha256"]) == 16


async def test_a_denied_call_does_not_run(tmp_path: Path) -> None:
    async def no(tool_name: str, reason: str) -> bool:
        return False

    audit = RecordingAudit()
    context = make_context(tmp_path, approver=CallbackApprover(no), audit=audit)
    result = await run_bash(context, "mkdir nope")

    assert result.is_error is True
    assert "declined" in result.content
    assert not (tmp_path / "nope").exists()
    assert "tool.result" not in audit.types()
    assert audit.of("tool.decision")[0]["approval"]["outcome"] == "denied"


async def test_an_unanswered_request_times_out_as_a_denial(tmp_path: Path) -> None:
    context = make_context(tmp_path, approver=ApprovalBroker(timeout=0.05))
    result = await run_bash(context, "mkdir slow")

    assert result.is_error is True
    assert "Nobody answered" in result.content
    assert not (tmp_path / "slow").exists()


async def test_an_outside_answer_unblocks_the_call(tmp_path: Path) -> None:
    broker = ApprovalBroker(timeout=5)
    context = make_context(tmp_path, approver=broker)

    running = asyncio.create_task(run_bash(context, "mkdir via-broker"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if broker.pending():
            break
    (request,) = broker.pending()
    assert request.tool_name == "bash"
    assert request.summary == "mkdir via-broker"
    assert request.session_id == "sess-1"
    broker.resolve(request.id, approved=True, decided_by="api:alice")
    result = await running

    assert result.is_error is False
    assert (tmp_path / "via-broker").is_dir()


async def test_a_crashing_approver_means_no(tmp_path: Path) -> None:
    class Broken:
        async def request(self, request):
            raise RuntimeError("approval service is down")

    context = make_context(tmp_path, approver=Broken())
    result = await run_bash(context, "mkdir broken")

    assert result.is_error is True
    assert not (tmp_path / "broken").exists()


async def test_approval_requests_carry_the_risk_of_the_call(tmp_path: Path) -> None:
    seen = []

    class Recorder:
        async def request(self, request):
            seen.append(request)
            return ApprovalDecision(ApprovalOutcome.DENIED, "test")

    context = make_context(tmp_path, approver=Recorder())
    await run_bash(context, "rm -rf build")

    (request,) = seen
    assert request.risk.label == "high"
    assert request.risk_reasons == ("deletes recursively",)
    assert request.summary == "rm -rf build"


async def test_secrets_in_the_command_do_not_reach_the_prompt_or_the_log(tmp_path: Path) -> None:
    seen = []

    class Recorder:
        async def request(self, request):
            seen.append(request)
            return ApprovalDecision(ApprovalOutcome.DENIED, "test")

    audit = RecordingAudit()
    context = make_context(tmp_path, approver=Recorder(), audit=audit)
    await run_bash(context, "curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://example.com")

    assert "abcdefghijklmnop1234" not in seen[0].summary
    assert "abcdefghijklmnop1234" not in str(audit.records)


# --- no approval needed ---------------------------------------------------------------------


async def test_read_only_calls_need_no_approval(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    audit = RecordingAudit()
    context = make_context(tmp_path, audit=audit)

    result = await _execute_tool_call(context, "read_file", "toolu_1", {"path": "a.txt"})

    assert result.is_error is False
    assert audit.types() == ["tool.decision", "tool.result"]
    assert audit.of("tool.decision")[0]["source"] == "policy"


async def test_full_auto_runs_without_asking_but_still_records(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO, audit=audit)
    result = await run_bash(context, "mkdir auto")

    assert result.is_error is False
    assert audit.types() == ["tool.decision", "tool.result"]
    assert audit.of("tool.decision")[0]["mode"] == "full_auto"


async def test_the_allow_list_skips_the_prompt(tmp_path: Path) -> None:
    context = make_context(tmp_path, allowed_tools=["Bash"])  # names are matched ignoring case
    result = await run_bash(context, "mkdir listed")
    assert result.is_error is False
    assert (tmp_path / "listed").is_dir()


async def test_the_deny_list_wins_over_full_auto(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO, audit=audit, denied_tools=["bash"])
    result = await run_bash(context, "mkdir nope")

    assert result.is_error is True
    assert not (tmp_path / "nope").exists()
    assert audit.of("tool.decision")[0]["source"] == "policy"


# --- calls that no approval can unlock ---------------------------------------------------------


async def test_critical_calls_are_refused_even_in_full_auto_and_even_if_approvable(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(
        tmp_path, mode=PermissionMode.FULL_AUTO, approver=AutoApprover(), audit=audit, allowed_tools=["bash"]
    )
    result = await run_bash(context, "cat ~/.aws/credentials")

    assert result.is_error is True
    assert "critical risk" in result.content
    assert result.result_metadata["denied_by"] == "policy"
    assert result.result_metadata["risk"] == "critical"
    assert audit.types() == ["tool.decision"]
    decision = audit.of("tool.decision")[0]
    assert decision["decision"] == "deny"
    assert decision["risk"] == "critical"
    assert decision["risk_reasons"]


async def test_credential_paths_are_refused_for_file_tools(tmp_path: Path) -> None:
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO)
    result = await _execute_tool_call(
        context, "read_file", "toolu_1", {"path": str(Path.home() / ".ssh" / "id_rsa")}
    )
    assert result.is_error is True
    assert "sensitive credential path" in result.content


# --- hooks and audit trouble ------------------------------------------------------------------------


class BlockingHooks:
    async def execute(self, event, payload):
        return AggregatedHookResult(results=[HookResult(hook_type="command", success=False, blocked=True, reason="policy says no")])


async def test_a_blocking_hook_is_recorded_as_the_decider(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO, audit=audit, hooks=BlockingHooks())
    result = await run_bash(context, "mkdir hooked")

    assert result.is_error is True
    assert result.content == "policy says no"
    assert not (tmp_path / "hooked").exists()
    decision = audit.of("tool.decision")[0]
    assert (decision["decision"], decision["source"]) == ("deny", "hook")


async def test_a_call_is_not_run_when_the_log_fails_closed(tmp_path: Path) -> None:
    audit = RecordingAudit(fail_on="tool.decision")
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO, audit=audit)
    result = await run_bash(context, "mkdir unlogged")

    assert result.is_error is True
    assert "audit log cannot be written" in result.content
    assert not (tmp_path / "unlogged").exists()


async def test_failing_to_record_a_refusal_does_not_change_the_refusal(tmp_path: Path) -> None:
    audit = RecordingAudit(fail_on="tool.decision")
    context = make_context(tmp_path, mode=PermissionMode.PLAN, audit=audit)
    result = await run_bash(context, "mkdir plan-blocked")
    assert result.is_error is True
    assert "Plan mode" in result.content


async def test_file_contents_are_logged_as_a_digest(tmp_path: Path) -> None:
    audit = RecordingAudit()
    context = make_context(tmp_path, mode=PermissionMode.FULL_AUTO, audit=audit)
    secret = "API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123"
    await _execute_tool_call(context, "write_file", "toolu_1", {"path": "cfg.env", "content": secret})

    logged = audit.of("tool.decision")[0]["input"]
    assert logged["path"] == "cfg.env"
    assert logged["content"]["chars"] == len(secret)
    assert secret not in str(audit.records)


# --- through the engine ----------------------------------------------------------------------------------


def make_engine(tmp_path: Path, client, **kwargs) -> QueryEngine:
    return QueryEngine(
        api_client=client,
        tool_registry=create_default_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(mode=PermissionMode.DEFAULT)),
        cwd=tmp_path,
        model="test-model",
        system_prompt="system",
        **kwargs,
    )


async def test_the_engine_reports_refusals_to_frontends(tmp_path: Path) -> None:
    client = ScriptedClient(tool_message(("bash", {"command": "mkdir x"})), text_message("could not"))
    engine = make_engine(tmp_path, client)

    events = [event async for event in engine.submit_message("make a dir")]

    (completed,) = [e for e in events if isinstance(e, ToolExecutionCompleted)]
    assert completed.is_error is True
    assert completed.metadata["denied"] is True
    assert completed.metadata["denied_by"] == "approval"
    assert not (tmp_path / "x").exists()


async def test_the_engine_still_accepts_the_legacy_permission_prompt(tmp_path: Path) -> None:
    asked = []

    async def yes(tool_name: str, reason: str) -> bool:
        asked.append(tool_name)
        return True

    client = ScriptedClient(tool_message(("bash", {"command": "mkdir y"})), text_message("done"))
    engine = make_engine(tmp_path, client, permission_prompt=yes)
    _ = [event async for event in engine.submit_message("make a dir")]

    assert asked == ["bash"]
    assert (tmp_path / "y").is_dir()


async def test_parallel_tool_calls_are_asked_about_one_at_a_time(tmp_path: Path) -> None:
    active = peak = 0

    async def slow_yes(tool_name: str, reason: str) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return True

    client = ScriptedClient(
        tool_message(("bash", {"command": "mkdir a"}), ("bash", {"command": "mkdir b"})),
        text_message("done"),
    )
    engine = make_engine(tmp_path, client, permission_prompt=slow_yes)
    _ = [event async for event in engine.submit_message("two dirs")]

    assert peak == 1
    assert (tmp_path / "a").is_dir() and (tmp_path / "b").is_dir()


async def test_tools_see_the_real_permission_state_of_the_session(tmp_path: Path) -> None:
    """tool_metadata carries a copy of the mode that only plan-mode tools update; the checker is the truth."""
    from pydantic import BaseModel

    from seawall.tools.base import BaseTool, ToolExecutionContext, ToolResult

    seen = {}

    class Probe(BaseTool):
        name = "probe"
        description = "records its context"

        class input_model(BaseModel):
            pass

        def is_read_only(self, arguments) -> bool:
            return True

        async def execute(self, arguments, context: ToolExecutionContext) -> ToolResult:
            seen.update(context.metadata)
            return ToolResult(output="ok")

    context = make_context(
        tmp_path, mode=PermissionMode.FULL_AUTO, allowed_tools=["glob"], denied_tools=["web_fetch"]
    )
    context.tool_registry.register(Probe())
    context.tool_metadata["permission_mode"] = "default"  # stale copy

    await _execute_tool_call(context, "probe", "toolu_1", {})

    assert seen["permission_mode"] == "full_auto"
    assert seen["permission_allowed_tools"] == ["glob"]
    assert seen["permission_denied_tools"] == ["web_fetch"]
