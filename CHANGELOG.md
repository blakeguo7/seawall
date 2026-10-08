# Changelog

All notable changes to Seawall are recorded in this file. The format is based on Keep a Changelog.

## [Unreleased]

### Added

- Approvals as a first-class step. A call that needs confirmation becomes an approval request (tool, redacted summary, risk label) answered by an approver: the terminal UI, an `ApprovalBroker` for outside callers such as an HTTP API, or nobody. No answer, a timeout (`permission.approval_timeout_seconds`) or an error all mean the call does not run. See [docs/permissions-and-audit.md](docs/permissions-and-audit.md).
- Risk labels (`low`, `medium`, `high`, `critical`) for every tool call, shown in approval prompts and recorded in the audit log. `critical` calls (wiping the disk or home directory, reading credential stores, reverse shells, fork bombs, shutdown) are refused in every mode, including `full_auto`.
- A tamper-evident audit log: one hash-chained JSON Lines file per session, optionally HMAC-signed, with secrets redacted and file contents recorded as digests. `seawall audit list | show | verify`, and the `audit` settings section.
- `--allowed-tools` and `--disallowed-tools` take effect (they were accepted and ignored), `--append-system-prompt` takes effect outside `--dry-run`, and unknown tool names are reported on startup.
- `seawall -p` reports refused calls: on stderr for `text`, as `permission_denials` in the `json` result, and as `denied` on `stream-json` tool events.
- Cost accounting for every model call the engine makes (turns, compaction summaries, memory extraction), including prompt-cache tokens, with a `pricing` setting and an approximate built-in price table. Models without a price are reported as unpriced, never guessed. `/cost` shows usage, estimated cost and limits.
- Run limits: `--max-budget-usd`, `--max-total-tokens` and `--max-seconds` (and the `limits` settings), checked before every model call. A dollar budget for a model with no price is refused instead of ignored.
- A loop guard that warns an agent repeating the same call with the same result, and stops it after five repeats or eight failures in a row.
- Every run ends with a `RunFinished` event: stop reason, turns, usage, cost. `seawall -p` puts it in its `json` and `stream-json` output and exits with 0 (completed), 1 (API error) or 2 (cut short by a limit). See [docs/limits-and-cost.md](docs/limits-and-cost.md).

- An evaluation harness, `seawall eval`. Coding tasks (a workspace, a prompt, hidden tests, a reference solution) run through the real `seawall` command in a throwaway `HOME`; `validate` proves each task fails as shipped and passes with its solution; results report pass rate, turns, tokens, cost and time per task, with several trials per task for variance; `compare` is a regression gate. 21 tasks ship in `evals/tasks`. Free scripted agents (an `oracle` that applies the reference solution, a `noop`, a `cheater` that rewrites tests) run the whole stack through a local Anthropic-compatible server; `--agent live` runs a real model and requires a per-trial budget. See [docs/evals.md](docs/evals.md).
- A safety suite of 19 scenarios in which a compromised model tries harmful calls (credential reads, `rm -rf ~`, prompt injection from a file, exfiltration, writes outside the workspace, edits to its own audit log) against the real harness, checking what the model was shown, what was damaged, what reached a canary server, and what the audit log decided.
- A CI job that validates the tasks, runs both suites and compares them with the committed baselines in `evals/baselines`.

- An HTTP service, `seawall serve` (Starlette and uvicorn, SSE streaming). Sessions, runs, numbered events, snapshots, approvals and leases live in SQLite (WAL); events are written before they are sent, so every stream is a cursor that resumes from `Last-Event-ID`, a slow client cannot slow the agent, and a second server on the same database can follow a run. Bounded concurrency (running slots, a queue, `429` with `Retry-After`), one run per session, interruption (model calls cancelled, shell commands killed), approvals answered with `POST` (silence refuses), a per-run time limit, bearer-token authentication (or loopback-only with Host and Origin checks), a workspace root that session directories must stay inside, caps on every limit a client may request, and refusal of unattended modes unless the operator opts in. Sessions and conversations survive restarts and crashes; session limits and cost carry over. See [docs/server.md](docs/server.md).

### Changed

- Users and the model see the name Seawall: `seawall --help` and `--version`, slash commands, the system prompt, the task-restart notice, the transcript export and the HTTP user agent. The command is `seawall` (short alias `sw`), the Python package is `seawall`, state lives in `~/.seawall`, the environment variables start with `SEAWALL_` and the distribution is named `seawall`.
- Cron jobs of kind `agent_turn` run `python -m seawall --print <message>` and select their provider profile through `SEAWALL_PROFILE`.
- The `image_generation` tool only supports an OpenAI-compatible key and base URL.
- `mcp` is pinned below 2.0.
- The ruff rule set is pinned to E4, E7, E9 and F.
- The terminal UI's permission prompt shows what is about to run and its risk label, not only the tool name.
- Sub-agent workers inherit the parent's permission mode and tool lists. In `default` mode a worker can read but not write; use `full_auto` or `--allowed-tools` for workers that edit.
- The built-in list of credential paths now also covers `.netrc`, `.pgpass`, `.git-credentials`, `.pypirc`, the GitHub CLI token, keychains, the password store and `/etc/shadow`.
- Tool names in the allow and deny lists are matched without regard to case.
- After a run ends for any reason, the conversation keeps the results of the tools that ran last. Previously they were dropped when the run stopped before another assistant message, so a stopped run could not be continued.
- Command-line session options (`--allowed-tools`, `--max-budget-usd`, ...) travel as one `SessionOverrides` object from the CLI to the runtime instead of as separate parameters through six functions.
- `UsageSnapshot` has cache-read and cache-write token fields.
- Tool input schemas are generated once per tool and reused. Building them took a few hundred microseconds per tool on every model call, which a process serving several sessions would spend on its event loop.
- `build_runtime` takes an optional `session_id`, `CostTracker.restore` loads saved totals, `session_storage.persistable_tool_metadata` is public, and `ui.runtime.prepare_submission` is the part of `handle_line` that readies the engine for a plain prompt.
- `pyproject.toml` has a `server` extra (starlette, uvicorn, sse-starlette), also in `dev`. `mcp` already brings them in.
- Tests run with the developer's `ANTHROPIC_*`, `OPENAI_*` and similar variables removed and with `HOME` pointed at a temporary directory, so they no longer write to `~/.seawall`. Every test starts in an empty temporary directory (looking up a project's plugins creates `<cwd>/.seawall`, and seven tests used to create it in the repository), and a session-wide check fails the run if any test changes the real `~/.seawall` or the repository's own `.seawall` anyway (for example one that calls `monkeypatch.undo()`, which also undoes the isolation).

### Fixed

- `grep` and `glob` pointed at a parent directory (for example the home directory) returned the contents or names of credential files such as `~/.ssh/id_rsa`; only the search root was checked. Found by the new safety suite. Both tools now drop credential files from their results, and a recursive `grep`/`rg` over a whole home or system directory in `bash` is labelled `high`.
- `seawall -p` ignored `--permission-mode` and approved every confirmation. It now runs under the requested mode and refuses what would need approval, because nobody is there to approve it.
- Sub-agent workers approved every confirmation themselves, whatever mode the parent was in. They now refuse what would need approval and run under the parent's mode.
- Shell commands could read credential files that the file tools were forbidden to read (`cat ~/.ssh/id_rsa`). The credential-path check now covers paths inside commands.
- `--append-system-prompt` in `--dry-run` replaced the system prompt instead of appending to it.
- `seawall eval` with a scripted agent failed with HTTP 502 on a Mac that has a system-wide proxy: the agent runs in a clean environment, httpx then falls back to the system proxy, and the proxy does not serve the local port. Scripted agents (and the safety suite, whose canary server is also local) now run with loopback excluded from proxying; live agents get the caller's proxy variables. Tests are likewise kept off the machine's proxy.
