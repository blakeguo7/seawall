"""Running the agent on one task: workspace, agent process, audit trail, grading."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from seawall.audit import read_records, verify_log
from seawall.evals.grader import GradeResult, clean_env, grade
from seawall.evals.scripted import SCRIPTED_AGENTS, SCRIPTED_MODEL, ScriptedServer
from seawall.evals.task import Task
from seawall.evals.workspace import diff_trees, hash_tree, materialize, protected_changes

# Variables a live agent needs from the caller's environment to reach its provider: its keys, and
# the proxy settings that may stand between it and the provider.
LIVE_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "DEEPSEEK_", "DASHSCOPE_")
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
# A scripted agent talks to a server on this machine, and the traffic must stay here. Without this a
# clean environment falls back to the system proxy on macOS, which does not know our local port and
# answers every request with 502.
LOOPBACK_DIRECT = {"NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"}
# What the scripted model "costs", so that cost columns are not all zero in CI runs.
SCRIPTED_PRICING = {SCRIPTED_MODEL: {"input": 3.0, "output": 15.0}}


@dataclass(frozen=True)
class AgentSpec:
    """Which agent to run: a scripted one (``oracle``, ``noop``, ``cheater``) or ``live``.

    A live agent talks to the provider configured for the user (or given here) and costs
    real money; give it a budget.
    """

    kind: str = "oracle"
    model: str | None = None
    base_url: str | None = None
    api_format: str | None = None
    permission_mode: str | None = None
    max_budget_usd: float | None = None
    max_total_tokens: int | None = None

    @property
    def is_live(self) -> bool:
        return self.kind == "live"

    def validate(self) -> None:
        if self.kind != "live" and self.kind not in SCRIPTED_AGENTS:
            raise ValueError(f"unknown agent {self.kind!r}; use one of: live, {', '.join(sorted(SCRIPTED_AGENTS))}")

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "model": self.model or (SCRIPTED_MODEL if not self.is_live else None),
            "permission_mode": self.permission_mode,
            "max_budget_usd": self.max_budget_usd,
            "max_total_tokens": self.max_total_tokens,
        }


@dataclass
class AgentProcessResult:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    result: dict[str, Any] | None
    duration_seconds: float


@dataclass
class AuditSummary:
    path: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    verified: bool = False

    def decisions(self) -> list[dict[str, Any]]:
        return [r["data"] for r in self.records if r.get("type") == "tool.decision"]

    @property
    def tool_calls_allowed(self) -> int:
        return sum(1 for d in self.decisions() if d.get("decision") == "allow")

    @property
    def tool_calls_denied(self) -> int:
        return sum(1 for d in self.decisions() if d.get("decision") == "deny")


@dataclass
class TrialResult:
    task_id: str
    trial: int
    passed: bool
    grader_passed: bool
    stop_reason: str
    turns: int = 0
    detail: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    grade_seconds: float = 0.0
    files_changed: int = 0
    tampered_files: list[str] = field(default_factory=list)
    tool_calls_allowed: int = 0
    tool_calls_denied: int = 0
    audit_verified: bool = False
    exit_code: int | None = None
    error: str | None = None
    grader_output_tail: str = ""
    run_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_tokens(self) -> int:
        return sum(self.usage.get(k, 0) for k in ("input_tokens", "output_tokens"))


def write_settings(home: Path, *, pricing: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> Path:
    """Write the isolated ``settings.json`` a trial's agent runs under."""
    config_dir = home / ".seawall"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {"memory": {"enabled": False}}
    if pricing:
        settings["pricing"] = pricing
    settings.update(extra or {})
    path = config_dir / "settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


async def run_agent_process(
    *,
    prompt: str,
    workspace: Path,
    home: Path,
    flags: list[str],
    env_extra: dict[str, str] | None = None,
    timeout: float,
) -> AgentProcessResult:
    """Run ``seawall -p`` in ``workspace`` under an isolated HOME and parse its JSON result."""
    workspace, home = workspace.resolve(), home.resolve()  # the child runs elsewhere; relative paths would break
    config_dir = home / ".seawall"
    env = clean_env(
        home,
        extra={
            "SEAWALL_CONFIG_DIR": str(config_dir),
            "SEAWALL_DATA_DIR": str(config_dir / "data"),
            **(env_extra or {}),
        },
    )
    command = [
        sys.executable,
        "-m",
        "seawall",
        "-p",
        prompt,
        "--cwd",
        str(workspace),
        "--output-format",
        "json",
        *flags,
    ]
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        out, err = await process.communicate()
    stdout = out.decode("utf-8", errors="replace")
    return AgentProcessResult(
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=err.decode("utf-8", errors="replace"),
        result=_last_json_object(stdout),
        duration_seconds=time.monotonic() - started,
    )


def _last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def read_audit(home: Path) -> AuditSummary:
    """Load and verify the session audit log the agent left under ``home``."""
    directory = home / ".seawall" / "data" / "audit"
    logs = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    if not logs:
        return AuditSummary()
    path = logs[-1]
    return AuditSummary(path=str(path), records=list(read_records(path)), verified=verify_log(path).ok)


def agent_flags(task: Task, agent: AgentSpec, *, base_url: str | None) -> tuple[list[str], dict[str, str]]:
    """The command-line flags and extra environment for a trial's agent process."""
    flags = ["--max-turns", str(task.max_turns)]
    env: dict[str, str] = {}
    if agent.is_live:
        if agent.model:
            flags += ["--model", agent.model]
        if agent.base_url:
            flags += ["--base-url", agent.base_url]
        if agent.api_format:
            flags += ["--api-format", agent.api_format]
        env = {k: v for k, v in os.environ.items() if k.startswith(LIVE_ENV_PREFIXES) or k in PROXY_ENV}
    else:
        flags += ["--model", SCRIPTED_MODEL, "--base-url", str(base_url), "--api-key", "sk-scripted"]
        env = dict(LOOPBACK_DIRECT)
    if agent.permission_mode:
        flags += ["--permission-mode", agent.permission_mode]
    else:
        flags += ["--allowed-tools", ",".join(task.allowed_tools)]
    if agent.max_budget_usd is not None:
        flags += ["--max-budget-usd", repr(agent.max_budget_usd)]
    if agent.max_total_tokens is not None:
        flags += ["--max-total-tokens", str(agent.max_total_tokens)]
    return flags, env


async def run_trial(
    task: Task,
    trial: int,
    agent: AgentSpec,
    *,
    out_dir: Path,
    server: ScriptedServer | None,
) -> TrialResult:
    """Run one trial: fresh workspace, the agent, then grading."""
    run_dir = out_dir.resolve() / task.id / f"trial-{trial}"
    workspace, home = run_dir / "workspace", run_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    before = materialize(task, workspace)
    write_settings(home, pricing=None if agent.is_live else SCRIPTED_PRICING)

    base_url: str | None = None
    if not agent.is_live:
        if server is None:
            raise ValueError("a scripted agent needs a ScriptedServer")
        base_url = server.register(f"{task.id}-{trial}", SCRIPTED_AGENTS[agent.kind](task))
    flags, env = agent_flags(task, agent, base_url=base_url)

    process = await run_agent_process(
        prompt=task.prompt, workspace=workspace, home=home, flags=flags, env_extra=env, timeout=task.timeout_seconds
    )
    (run_dir / "agent-stdout.txt").write_text(process.stdout)
    (run_dir / "agent-stderr.txt").write_text(process.stderr)

    changes = diff_trees(before, hash_tree(workspace))
    tampered = protected_changes(task, workspace, before)
    audit = read_audit(home)
    graded: GradeResult = await grade(task, workspace, home)

    result = process.result or {}
    error = None
    if process.timed_out:
        error = f"agent did not finish within {task.timeout_seconds:g}s"
    elif process.result is None:
        error = f"agent produced no JSON result (exit {process.exit_code}): {process.stderr[-300:].strip()}"
    stop_reason = "timeout" if process.timed_out else str(result.get("stop_reason", "no_result"))
    return TrialResult(
        task_id=task.id,
        trial=trial,
        passed=graded.passed and not tampered,
        grader_passed=graded.passed,
        stop_reason=stop_reason,
        turns=int(result.get("turns", 0)),
        detail=str(result.get("detail", "")),
        usage=dict(result.get("usage", {})),
        cost_usd=result.get("cost_usd"),
        duration_seconds=round(process.duration_seconds, 3),
        grade_seconds=round(graded.duration_seconds, 3),
        files_changed=changes.total,
        tampered_files=tampered,
        tool_calls_allowed=audit.tool_calls_allowed,
        tool_calls_denied=audit.tool_calls_denied,
        audit_verified=audit.verified,
        exit_code=process.exit_code,
        error=error,
        grader_output_tail="" if graded.passed else graded.output_tail,
        run_dir=str(run_dir),
    )


async def validate_task(task: Task, scratch: Path) -> list[str]:
    """Problems with a task, or an empty list when it is sound.

    A sound task fails grading as shipped and passes once the reference solution is applied.
    """
    from seawall.evals.workspace import overlay_solution

    problems: list[str] = []
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)

    workspace = scratch / "as-shipped"
    materialize(task, workspace)
    shipped = await grade(task, workspace, home)
    if shipped.passed:
        problems.append("grading passes on the untouched repo, so the task proves nothing")
    elif shipped.timed_out:
        problems.append("grading timed out on the untouched repo")

    solved_workspace = scratch / "solved"
    materialize(task, solved_workspace)
    overlay_solution(task, solved_workspace)
    solved = await grade(task, solved_workspace, home)
    if not solved.passed:
        problems.append("grading fails with the reference solution applied:\n" + solved.output_tail.strip())
    return problems
