# Seawall

A compact coding-agent harness in Python. It has the parts a coding agent needs and a set of controls around them: the agent loop, a tool layer, permissions and hooks, approvals with a tamper-evident audit log, cost and run limits, an evaluation harness, an HTTP service, context compaction, memory, MCP, sub-agents, an optional sandbox and a terminal UI, all behind one `seawall` command.

A seawall stands between a harbour and the sea. It does not calm the sea; it decides how far the water can reach. Approvals, limits, the audit log and interrupt do that for a coding agent: they bound what a bad step can do and leave a record of it. They are controls on what the agent does, not isolation from the machine it runs on; [docs/permissions-and-audit.md](docs/permissions-and-audit.md) says exactly what they do and do not guarantee.

It is limited to the coding-agent core on purpose, so that the whole code base can be read and owned. [Scope](#scope) says what is in and what is left out.

## Status

The core is finished and tested (frontend type-check, an end-to-end run against a fake model endpoint, and about 1,400 unit and integration tests). Approvals, the audit log, cost accounting, run limits, the loop guard, an evaluation harness and an HTTP service are done; what is not built yet is listed under [Planned](#planned).

## Quick start

Requirements: Python 3.10+, and Node.js 18+ for the interactive terminal UI.

```bash
git clone https://github.com/blakeguo7/seawall.git
cd seawall
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # or: uv sync --extra dev
(cd frontend/terminal && npm ci)                              # terminal UI dependencies

.venv/bin/seawall setup        # pick a provider and store an API key
.venv/bin/seawall              # interactive session
```

Non-interactive use:

```bash
seawall -p "Summarize what src/seawall/engine does"                 # one prompt, text output
seawall -p "List the permission modes" --output-format json             # structured result
seawall -p "Fix the failing test" --output-format stream-json           # events as they happen
seawall -p "Fix the failing test" --allowed-tools bash,edit_file        # headless runs cannot ask; list what may run
seawall -p "Fix the failing test" --permission-mode full_auto --max-budget-usd 2   # stop before the next call once $2 is spent
seawall serve --workspace ~/projects                                   # the agent as an HTTP service (token from SEAWALL_SERVER_TOKEN)
seawall eval run --agent oracle --expect-all-pass                       # the coding suite with a scripted agent (free)
seawall eval run --agent live --model my-model --max-budget-usd 0.5     # the same tasks with a real model
seawall -p "Refactor utils" --permission-mode plan --dry-run            # preview the setup, run nothing
```

`seawall` and `sw` are the same command. The Python package is `seawall`; its state lives in `~/.seawall` and its environment variables start with `SEAWALL_`.

## Providers

The harness talks to two API families: Anthropic's Messages API and OpenAI-compatible chat APIs. Authentication is by API key only.

Built-in profiles: `claude-api` (Anthropic and Anthropic-compatible endpoints), `openai-compatible`, and `qwen` (Alibaba DashScope). `seawall setup` also offers a DeepSeek preset (Anthropic-compatible endpoint). Anything else is a custom profile:

```bash
seawall provider add my-gateway --label "My gateway" --provider openai --api-format openai \
  --auth-source openai_api_key --base-url https://example.com/v1 --model my-model \
  --api-key "$MY_GATEWAY_KEY"
seawall provider use my-gateway
seawall auth status
```

Keys can also come from the environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`, or the `SEAWALL_`-prefixed variants). Stored keys live in `~/.seawall/credentials.json` (mode 600) or the system keyring when available. Settings are in `~/.seawall/settings.json`; use `SEAWALL_CONFIG_DIR` and `SEAWALL_DATA_DIR` to relocate them.

## Architecture

```
seawall (cli.py)
 ├─ ui/            runtime assembly; JSON-lines backend for the React terminal UI, headless print mode
 ├─ engine/        QueryEngine and the agent loop (query.py): model call → tool calls → results → repeat
 ├─ api/           Anthropic client and OpenAI-compatible client, retry with backoff, provider detection
 ├─ tools/         39 tools: files, shell, search, web, tasks, cron, plan mode, sub-agents, MCP, skills
 ├─ permissions/   modes (default, plan, full_auto), allow/deny lists, credential-path deny list, risk labels, approvals
 ├─ audit/         tamper-evident per-session audit log, secret redaction, `seawall audit`
 ├─ evals/         `seawall eval`: coding tasks, safety scenarios, scripted agents, reports, regression gate
 ├─ server/        `seawall serve`: HTTP + SSE service, SQLite sessions and events, leases, approvals over the API
 ├─ hooks/         10 lifecycle events (pre/post tool use, session start/end, compaction, ...); command, prompt, HTTP and agent hooks
 ├─ services/      context compaction, memory consolidation ("dream"), cron scheduler, session storage
 ├─ memory/        project memory files with relevance selection and usage tracking
 ├─ mcp/           MCP client (stdio and HTTP servers)
 ├─ sandbox/       optional isolation for shell commands: sandbox-runtime or Docker
 ├─ tasks/ swarm/ coordinator/   background tasks; sub-agents run as subprocess workers; coordinator mode
 ├─ skills/ plugins/             markdown skills (bundled and user) and plugin packages
 └─ commands/      60+ slash commands (/compact, /resume, /permissions, /mcp, /provider, /diff, ...)
frontend/terminal  React + Ink terminal UI, talks to the Python backend over stdin/stdout
```

The loop in [`engine/query.py`](src/seawall/engine/query.py) is the place to start reading. A turn sends the conversation to the model, executes the tool calls it returns (checking permissions and running hooks around each one), appends the results, compacts the context when it gets too large, and stops when the model answers without asking for a tool.

Service: `seawall serve` runs the same runtime behind HTTP. Clients create sessions, send messages, follow the run as a resumable event stream (SSE, numbered events kept in SQLite, `Last-Event-ID` supported), answer approval requests, interrupt runs, and read the audit log. Several sessions run at once under a concurrency cap with a queue and `429` beyond it; sessions survive restarts; clients can only ask for what the operator allows (workspace root, permission mode, limits), and cannot supply credentials. There is no sandbox and no multi-tenancy: one token, tools run as the server's user. Details, API and limits: [docs/server.md](docs/server.md).

Evaluation: `seawall eval` runs 21 coding tasks (hidden tests, reference solutions, a check that each task fails as shipped and passes when solved) and 19 safety scenarios (a compromised model against the safety layers) through the real command, and reports pass rate, turns, tokens, cost and time. Scripted agents make both suites free and deterministic enough to gate CI; `--agent live` measures a real model under a per-trial budget. Details and what the scores do and do not show: [docs/evals.md](docs/evals.md).

Cost and limits: every model call the engine makes is metered, `/cost` shows the estimated spend, and a run can be bounded by dollars, tokens, time or turns. An agent that repeats the same call without getting anywhere is warned and then stopped. `seawall -p` reports the stop reason, usage and cost in its output and exits 2 when a limit cut the run short. Prices are configuration (the built-in table is approximate and small); a dollar budget for a model with no known price is refused rather than ignored. Details: [docs/limits-and-cost.md](docs/limits-and-cost.md).

Permissions: `default` asks before anything that writes or runs commands, `plan` blocks all writes, `full_auto` allows everything that is not explicitly denied. Credential files (`~/.ssh`, `~/.aws`, keychains, ...) and `critical` commands (`rm -rf ~`, reverse shells, ...) are refused in every mode. A call that needs approval and has nobody to ask, as in `seawall -p` or a sub-agent, is refused rather than approved, and every decision lands in a hash-chained audit log. Details, guarantees and limits: [docs/permissions-and-audit.md](docs/permissions-and-audit.md).

## Scope

Seawall covers the coding-agent core and leaves the rest out on purpose:

- No chat-app gateway (Slack, Telegram, Discord, Feishu, ...) and no personal-assistant agent.
- No bot that turns GitHub issues into pull requests.
- API keys only: no login with a Claude Code, Codex or GitHub Copilot subscription.
- Built-in provider profiles for Anthropic, OpenAI, DeepSeek and DashScope; anything else is a custom profile.
- Sub-agents run as subprocess workers; there are no in-process teammates, terminal-multiplexer panes, mailboxes or worktree management.
- One terminal UI (React and Ink), without themes, output styles, vim or voice mode.

The Python code is about 36,000 lines in 203 files, with about 22,000 lines of tests and a 3,400-line TypeScript terminal UI.

## Planned

Not implemented yet, in the order they are likely to be built:

- A tighter Docker sandbox: no network by default, resource limits, a throwaway workspace per session. (The HTTP service refuses to start with the current Docker sandbox enabled, because it is one container per process; a per-session sandbox would lift that.)

## Development

```bash
.venv/bin/python -m pytest -q                      # unit and integration tests, no network needed
.venv/bin/ruff check src tests scripts
(cd frontend/terminal && npx tsc --noEmit)         # frontend type-check
```

Tests that talk to a model use a fake client; nothing in `tests/` needs an API key.

## License

MIT, see [LICENSE](LICENSE).
