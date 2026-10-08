# The HTTP service

`seawall serve` exposes the agent over HTTP: a client creates a **session**, sends it **messages**, follows what the agent does as a stream of **events**, and answers its **approval** requests. It is the same runtime the terminal UI and `seawall -p` use (same tools, permissions, limits and audit log), with a database under it so that sessions survive restarts and several can run at once.

```bash
export SEAWALL_SERVER_TOKEN=$(openssl rand -hex 24)      # 16 characters or more
seawall serve --workspace ~/projects --max-budget-usd 2 --max-turns 30
```

```bash
B=http://127.0.0.1:8765; T="Authorization: Bearer $SEAWALL_SERVER_TOKEN"

curl -s -X POST $B/v1/sessions -H "$T" -H 'Content-Type: application/json' \
     -d '{"title": "notes", "cwd": "demo"}'                              # -> {"id": "d58f90e51d55", ...}

curl -s -N -X POST $B/v1/sessions/d58f90e51d55/messages -H "$T" \
     -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
     -d '{"text": "What do my notes say?"}'                              # streams the run's events
```

## What it is for, and what it is not

It is a service layer: sessions with persistent history, concurrent runs with bounds, streaming with resume, approvals answered over the API, and an audit endpoint. It is meant to be driven by a program (a web back end, a bot, a job runner).

It is **not** a sandbox and **not** multi-tenant. Every tool runs with the server process's own privileges, and one token gives access to every session. See [Security](#security).

## Concepts

- **Session**: a conversation bound to a working directory (inside the workspace root the operator chose), with its own permission mode, tool lists and limits. Stored in the database; its runtime (engine, tools, MCP connections, audit log) is built when a message arrives and cached while the session is in use.
- **Run**: one message being processed, from the user's text to the model's final answer. A session has at most one run at a time.
- **Event**: something that happened during a run. Events are numbered per session, stored, and delivered over SSE.
- **Approval**: a tool call the permission policy wants a human to decide. The run waits for an answer from the API.

## Starting the server

```
seawall serve --workspace DIR [options]
```

| Option | Default | |
| --- | --- | --- |
| `--workspace` | (required) | Directory sessions may work in. A session's `cwd` must resolve to a place inside it (symlinks are followed before the check). |
| `--host`, `--port` | `127.0.0.1`, `8765` | |
| `--db` | `<data dir>/server/sessions.db` | SQLite file for sessions, events and snapshots. Created with mode 0600. |
| `--token-env` | `SEAWALL_SERVER_TOKEN` | Name of the environment variable holding the bearer token. There is deliberately no `--token`: arguments are visible in `ps`. |
| `--insecure-no-auth` | off | Serve without a token. Refused unless the address is loopback. |
| `--allow-full-auto` | off | Let clients start sessions that run tools without approval. See [Permissions](#permissions). |
| `--max-concurrent-runs` | 4 | Runs executing at once, across all sessions. |
| `--max-queued-runs` | 16 | Runs that may wait for a free slot. Beyond that, `429`. |
| `--max-loaded-sessions` | 16 | Session runtimes kept in memory. Idle ones are closed, least recently used first; their history is in the database. Must be at least `--max-concurrent-runs`. |
| `--max-sessions` | 1000 | Sessions the server stores. |
| `--max-budget-usd`, `--max-total-tokens`, `--max-seconds`, `--max-turns` | none | Caps on what a session may ask for, and the defaults for sessions that ask for nothing. |
| `--max-message-chars` | 100000 | Longest message accepted. |
| `--approval-timeout` | 300 | Seconds before an unanswered approval is refused. |
| `--run-timeout` | 3600 | Hard wall-clock limit on one run. |
| `--log-level` | `info` | |

The server uses **its own** model credentials, from `settings.json` and the provider environment variables, exactly as `seawall` does. A client cannot supply a key or a base URL. The Docker sandbox cannot be enabled while serving: it is one container per process, which cannot serve concurrent sessions, so the server refuses to start with `sandbox.enabled`.

There is no TLS. For anything beyond loopback, put the server behind a reverse proxy that terminates TLS (and, for streams, does not buffer: responses carry `X-Accel-Buffering: no` for nginx).

## Authentication

Everything under `/v1` needs `Authorization: Bearer <token>`; the comparison is constant-time. `GET /healthz` does not.

Without a token (`--insecure-no-auth`, loopback only) the server instead refuses requests that look like they come from a web page: a `Host` header that is not a loopback name (DNS rebinding) and any request with an `Origin` header. Without that, a page the user happened to open could drive an agent on their machine through `http://localhost:8765`.

There is no CORS. Browsers cannot call the API directly; use a back end, or a proxy you configure for it. `EventSource` cannot send an `Authorization` header either; read streams with `fetch`.

Every response carries `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

## API

All bodies are JSON (`Content-Type: application/json`). Errors have one shape, with a stable `code`:

```json
{"error": {"code": "busy", "message": "the session is still processing a message; ..."}}
```

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness: `{"status": "ok", "version": ...}`. No token needed. |
| `GET /v1/status` | Load and limits: running and queued runs, loaded sessions, open streams, the configured bounds. |
| `POST /v1/sessions` | Create a session. The body is optional. `201` with the session and a `Location` header. |
| `GET /v1/sessions?limit=&offset=` | List sessions, most recently updated first: `{"sessions": [...], "total": n}`. |
| `GET /v1/sessions/{id}` | One session: state, working directory, spec, usage, cost, last stop reason. |
| `DELETE /v1/sessions/{id}` | Delete the session, its events, snapshot, approvals and runs. `409` while a run is in progress. The audit log is kept. |
| `POST /v1/sessions/{id}/messages` | Send a message `{"text": ...}`; see [Sending messages](#sending-messages). |
| `GET /v1/sessions/{id}/messages` | The conversation as of the end of the last finished run: `{"messages": [...]}`. |
| `POST /v1/sessions/{id}/interrupt` | Stop the current run: `{"interrupted": true}`, or `false` if nothing was running. |
| `GET /v1/sessions/{id}/events` | The event stream (SSE); see [Events](#events). |
| `GET /v1/sessions/{id}/approvals?status=` | Approval requests, optionally only `pending`. |
| `POST /v1/sessions/{id}/approvals/{approval_id}` | Answer one: `{"approved": true, "note": "..."}`. |
| `GET /v1/sessions/{id}/runs?limit=` | Recent runs with their status, stop reason and turns. |
| `GET /v1/sessions/{id}/audit?tool=&decision=&min_risk=&limit=` | The session's audit records (the newest `limit`), with the result of checking the hash chain. |

### Creating a session

```json
{
  "title": "notes",
  "cwd": "demo",
  "model": "claude-sonnet-4-6",
  "permission_mode": "default",
  "allowed_tools": [],
  "disallowed_tools": ["bash"],
  "append_system_prompt": "Answer in one paragraph.",
  "max_budget_usd": 0.5,
  "max_total_tokens": 200000,
  "max_seconds": 300,
  "max_turns": 20
}
```

Every field is optional and unknown fields are rejected (`422`). `cwd` is relative to the workspace root (or absolute, but inside it). `permission_mode` is `default` (the default), `plan`, or `full_auto` if the operator allows it.

The session starts in `default` mode **whatever `settings.json` says**: a `full_auto` default in the operator's own settings is not inherited by API sessions.

The response is the session:

```json
{"id": "d58f90e51d55", "title": "notes", "cwd": "/srv/projects/demo", "state": "idle", "spec": {...},
 "created_at": 1791349387.7, "updated_at": 1791349387.7,
 "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
 "cost_usd": 0.0, "unpriced_models": [], "last_stop_reason": null}
```

`state` is `idle`, `queued` (a run is waiting for a slot) or `running`. `cost_usd` is `null` when a model has no known price (see [limits-and-cost.md](limits-and-cost.md)). Unknown tool names in `allowed_tools` or `disallowed_tools` do not fail the request; they come back in a `warnings` list, because a typo in a deny list would otherwise silently block nothing.

### Sending messages

One endpoint, three ways to get the answer, chosen by how the request is made:

| Request | Response |
| --- | --- |
| `POST .../messages` | `202` and `{"run_id", "session_id", "events_url"}`. Follow the run at `events_url`. |
| `POST .../messages?wait=1` | Held until the run ends, then `200` with the result: `run_id`, `stop_reason`, `detail`, `turns`, `duration_seconds`, this run's `usage` and `cost_usd`, and `text` (the last thing the assistant said). |
| `POST .../messages` with `Accept: text/event-stream` | The run's events, streamed on this response. |

**The run does not depend on the connection.** If the client goes away, the run carries on, and its events can be read later from the stream endpoint. Use `interrupt` to stop it.

Refusals: `404` unknown session, `409 busy` (the session already has a run; wait for `run.finished` or interrupt), `409 session_elsewhere` (another server process is running this session), `413` message too long, `422` invalid body, `429 overloaded` (all slots and the queue are taken; `Retry-After` is set), `503` shutting down.

### Events

`GET /v1/sessions/{id}/events` is a Server-Sent Events stream. Each event has an SSE `id` (its number in the session, increasing and gap-free), an `event` name, and a JSON `data`:

```
id: 5
event: tool.completed
data: {"seq": 5, "ts": 1791349387.9, "type": "tool.completed", "session_id": "d58f90e51d55",
       "data": {"run_id": "e56d4d86a072", "tool_name": "read_file", "output": "...", "is_error": false,
                "denied": false, "denied_by": null}}
```

Query parameters: `after=N` (only events after number `N`; the `Last-Event-ID` header does the same, which is what makes browsers' and libraries' automatic reconnection resume correctly) and `until_run=ID` (end the stream after that run's `run.finished`). Without `until_run` the stream stays open, with a `: ping` comment every 15 seconds, until the client leaves, the session is deleted, or the server shuts down.

| Event | `data` fields (besides `run_id`) |
| --- | --- |
| `run.started` | `text`: the user's message |
| `assistant.delta` | `text`: a piece of the answer. Pieces that arrive faster than they can be stored are joined, so the grouping varies |
| `assistant.message` | `text`, `tool_calls` (`id`, `name`), `usage` of that model call. This is the authoritative text of the turn |
| `tool.started` | `tool_name`, `tool_input`: redacted, with file contents reduced to a length and a digest |
| `tool.completed` | `tool_name`, `output` (cut at 20,000 characters), `is_error`, `denied`, `denied_by` |
| `approval.requested` | `id`, `tool_name`, `summary`, `reason`, `risk`, `risk_reasons`, `created_at` |
| `approval.resolved` | `id`, `status`, `decided_by`, `note` |
| `status`, `compact`, `error` | progress messages from the engine |
| `run.finished` | `stop_reason`, `detail`, `turns`, `duration_seconds`, `usage` and `cost_usd` of this run |

`run.finished` is the last event of every run, and it is sent **after** the session is idle again: a client that sees it can send its next message at once. `stop_reason` is `completed`, `error`, `max_turns`, `budget_exceeded`, `token_limit`, `time_limit`, `loop_detected`, `no_price` or `interrupted`.

**Resuming.** Events are written to the database before they are sent, and every subscriber is just a cursor into that table. A client that lost its connection reconnects with the last id it saw and gets exactly what it missed; a slow client cannot slow the agent or lose events, because nothing is buffered per client. The grouping of `assistant.delta` pieces is decided when they are stored, so a resumed stream can group the same text differently from what a live one showed; the text is the same, and `assistant.message` carries the whole turn.

Events are kept until the session is deleted.

### Approvals

In `default` mode a call that needs confirmation (writing a file, running most shell commands) does not run until someone answers. The run pauses and an `approval.requested` event appears:

```bash
curl -s "$B/v1/sessions/$SID/approvals?status=pending" -H "$T"
# {"approvals": [{"id": "9f2c1d7a3b40", "tool_name": "write_file", "summary": "path=made.txt",
#                 "risk": "medium", "risk_reasons": [...], "status": "pending", ...}]}

curl -s -X POST "$B/v1/sessions/$SID/approvals/9f2c1d7a3b40" -H "$T" \
     -H 'Content-Type: application/json' -d '{"approved": true, "note": "fine"}'
```

An approval ends as `approved`, `denied`, `timed_out` (nobody answered within `--approval-timeout`; the call is refused), `cancelled` (the run was interrupted while waiting) or `expired` (the server restarted while it was waiting). Answering twice gives `409 already_resolved`. An approval can only be answered by the server that is running the session (`409 not_pending_here` from another one).

Calls labelled `critical` are not asked about: they are refused in every mode. The model sees the refusal as the tool's result and can try something else.

### Interrupting

`POST .../interrupt` cancels the current call to the model or the running tool (a shell command is killed), closes any pending approval, and ends the run with `stop_reason: "interrupted"`. The conversation keeps everything up to that point.

## Permissions

What a client may ask for is bounded by the operator:

- **Working directory**: `cwd` must resolve inside `--workspace`. This decides where the session *starts*. It does not confine the tools: with permission, `bash` can still `cd` elsewhere. Containing that is the job of a sandbox, which this server does not provide.
- **Unattended execution**: `permission_mode: "full_auto"` and any non-empty `allowed_tools` (pre-approving `bash` is as strong as `full_auto` for shell commands) are refused with `403 unattended_disabled` unless the server was started with `--allow-full-auto`. `plan` mode and `disallowed_tools` only narrow a session and are always allowed.
- **Limits**: a session may ask for lower limits than the server's caps, never higher (`422 limit_too_high`). If the operator set a cap and the client asked for nothing, the cap applies.
- **Credentials and the model endpoint** cannot be set by a client. The `model` field chooses among whatever the server's provider accepts, so bound spending with `--max-budget-usd`.

Everything in [permissions-and-audit.md](permissions-and-audit.md) applies unchanged: credential paths and `critical` calls stay refused in every mode, the deny list beats everything, and silence is a refusal.

Slash commands are not available through the API (a message that starts with `/` is just text for the model): commands like `/clear` or `/model` change settings on disk and have no place in a multi-session server.

## Limits and what happens at them

| Situation | Result |
| --- | --- |
| A run reaches a session limit (budget, tokens, turns, time) or loops | It ends with that `stop_reason`; the session is fine and can take another message. Limits count the whole session across messages (except time and turns, which are per message), and survive restarts and eviction |
| A run goes past `--run-timeout` | Interrupted with `stop_reason: "time_limit"` |
| All slots and the queue are full | `429` with `Retry-After`, nothing is registered |
| The model's provider fails | The run ends with `stop_reason: "error"`; the server carries on |
| The server has no usable model credentials | Runs end with `error` ("no usable model credentials"); the server stays up |
| The event log cannot be written (disk full) | The run ends with `error` |

## Operation

**Persistence.** After every run the conversation, usage and cost are saved. A crash in the middle of a run loses that run's partial work: the session returns to the state after its last finished run. When the server starts it closes what a previous process left half done: such runs get a `run.finished` with `stop_reason: "interrupted"`, pending approvals become `expired`, and the sessions become idle.

**Shutdown** (SIGINT or SIGTERM): the server stops accepting work, wakes and closes the open streams, interrupts running runs (each ends with `interrupted` and "the server is shutting down"), saves, closes the runtimes (which ends their audit logs with `session.end`) and exits.

**Several processes on one database.** Point two servers at the same `--db` (a local disk; SQLite's locking does not work over network file systems). A session is run by one process at a time: the process holds a lease in the database, renews it every third of its length (60 seconds by default) and releases it when the run ends; if it dies, the lease expires and another process can take the session over. Reads, event streams and the audit endpoint work from any process (a stream served by a process that is not running the run finds new events by polling the database, within a second). A message for a session another process is running gets `409 session_elsewhere`. Event numbering is made safe across processes by SQLite's write lock. This is tested with two servers in one process; across real processes it rests on the same file locking.

**The audit log** is separate from the database: one hash-chained file per session in the audit directory ([permissions-and-audit.md](permissions-and-audit.md)), continuous across restarts. `GET .../audit` returns its records and whether the chain verifies. Deleting a session does not delete its audit log.

**Monitoring.** `GET /healthz` for liveness, `GET /v1/status` for load. Logs go to stderr; they contain request lines and errors, not message text or tokens.

## Security

What the service does:

- Requires a token (constant-time compare), or on loopback refuses anything that looks like it came from a web page.
- Keeps clients away from credentials, the model endpoint and the sandbox settings, and bounds the working directory, the permission mode and every limit.
- Reads request bodies with a size limit, validates every field, and never builds file paths from client input (session ids are generated, and a malformed id is a plain `404`).
- Applies the permission layers, the risk labels and the audit log to every tool call; refuses unattended execution unless the operator opts in.

What it does **not** do, so that nobody assumes it:

- **No sandbox.** Tools run as the server's user. A client with `full_auto` (or an approving human) can run any command that user can. Run the server as an unprivileged user in a container or VM if the clients are not fully trusted.
- **One token, one tenant.** Any holder of the token can read, change and delete every session, and see every event and audit record.
- **Sensitive data at rest.** The database holds conversations and tool outputs verbatim (the audit log redacts; the database does not). Protect the file (it is created 0600) and the data directory.
- **No rate limiting** beyond the concurrency and queue bounds, and no per-client quotas.
- **Not hardened against a hostile model.** The risk labels are a tripwire, not containment (see [permissions-and-audit.md](permissions-and-audit.md)).

## Limits of the implementation

- Building a session's runtime and assembling its prompt take about 20 ms each of CPU and run on the event loop (prompt assembly runs in a thread); with many MCP servers configured, loading a session takes longer. The agent loop, the model streams and the database work do not block the loop.
- Event streams are cursors into SQLite. They scale to hundreds of subscribers; this is not a message broker.
- Tested with Python 3.14, starlette 1.7, uvicorn 0.54 and sse-starlette 3.5. CI runs the same tests on 3.10 and 3.11; the code avoids newer-only features, but those runs are the evidence.

## A Python client

```python
import json, httpx

BASE, TOKEN = "http://127.0.0.1:8765", "..."
# trust_env=False: this is a local server. httpx would otherwise send even loopback requests
# through a system-wide proxy (macOS has one when a proxy app is on), and the proxy answers 502.
client = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=None, trust_env=False)

session = client.post("/v1/sessions", json={"title": "demo"}).json()

# stream one run; the connection can drop and be resumed from the last id
with client.stream("POST", f"/v1/sessions/{session['id']}/messages",
                   json={"text": "List the files here"}, headers={"Accept": "text/event-stream"}) as stream:
    event = None
    for line in stream.iter_lines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1])
            if event == "assistant.delta":
                print(data["data"]["text"], end="", flush=True)
            elif event == "approval.requested":
                approval = data["data"]
                print(f"\n[approve {approval['tool_name']}: {approval['summary']}?]")
                client.post(f"/v1/sessions/{session['id']}/approvals/{approval['id']}", json={"approved": False})
            elif event == "run.finished":
                print("\n", data["data"]["stop_reason"], data["data"]["usage"])
```
