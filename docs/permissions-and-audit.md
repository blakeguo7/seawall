# Permissions, approvals and the audit log

An agent that runs shell commands needs three things around every tool call: a decision about whether it may run, a way to ask a person when the decision is "ask", and a record of what happened. This page describes how Seawall does each, what the guarantees are, and where they stop.

## How a tool call is decided

Every call goes through the same steps in [`engine/query.py`](../src/seawall/engine/query.py), in this order. The first step that applies wins.

1. **Hooks.** A `pre_tool_use` hook can block the call.
2. **Credential paths.** A path that matches a built-in credential pattern (`~/.ssh`, `~/.aws`, `~/.kube`, keychains, `/etc/shadow`, ...) is refused. No mode, allow list or approval unlocks it. This applies to file tools and to paths that appear inside a shell command.
3. **Critical risk.** The call is labelled `low`, `medium`, `high` or `critical` (see below). `critical` is refused outright, again in every mode.
4. **Policy.** The tool deny list, then the tool allow list, then path rules and denied command patterns, then the permission mode:

   | Mode | Behaviour |
   | --- | --- |
   | `plan` | Reads only; everything that writes is refused. |
   | `default` | Reads run. Anything that writes or runs commands needs approval. |
   | `full_auto` | Everything runs, except what steps 1 to 3 and the deny rules refuse. |

5. **Approval**, when the policy says "ask" (see below).
6. **Audit.** The decision is written to the log *before* the tool runs, and the outcome after.

### Risk labels

The label comes from reading the command (or the file path) in [`permissions/risk.py`](../src/seawall/permissions/risk.py). It is shown in the approval prompt and written to the log, so a reviewer sees "recursive delete" and not just `bash`.

| Level | Examples | Effect |
| --- | --- | --- |
| `critical` | `rm -rf /` or `~`, writing to a disk device, `mkfs`, fork bombs, reverse shells, `shutdown`, reading `~/.aws/credentials` or the keychain, `kill -9 -1` | refused in every mode |
| `high` | `rm -rf build`, `sudo`, `curl ... \| sh`, `git push --force`, `git reset --hard`, editing `~/.zshrc` or `.git/hooks`, `ssh`, uploads with `curl -d` | label only |
| `medium` | `rm file`, `pip install`, `git push`, `curl`, writes outside the working directory | label only |
| `low` | everything else | |

Only `critical` changes a decision. The rest is information for the person approving and for whoever reads the log later.

**This is a tripwire, not a sandbox.** It tokenizes the command and matches known-dangerous shapes, including commands hidden in `bash -c`, `$(...)`, here-documents and `echo ... | sh`. It does not see what a script does (`python cleanup.py`), an encoded payload, or a path held in a variable it cannot resolve. A caller who wants to get around it can. What actually contains a command is isolation of the process, which is a separate piece of work (see the sandbox item in the README).

## Approvals

When the policy says "ask", the loop builds an approval request (tool, a redacted one-line summary of the call, the risk label and reasons) and gives it to an *approver*, defined in [`permissions/approvals.py`](../src/seawall/permissions/approvals.py). The answer is `approved`, `denied`, `timed_out` or `unavailable`. Only `approved` runs the call.

The rule that matters: **silence is a refusal.** No approver, no answer within `permission.approval_timeout_seconds` (default 300), an approver that raises, a cancelled session: all of them mean the call does not run.

| Approver | Used by |
| --- | --- |
| terminal UI | the interactive session: a prompt showing the command, the risk and the reasons |
| `DenyApprover` | `seawall -p` and sub-agent workers: nobody can answer |
| `ApprovalBroker` | anything that answers from outside the loop, such as an HTTP API; requests wait in a queue until resolved or timed out |
| `AutoApprover` | tests, or code that opts in explicitly |

### Headless runs

`seawall -p` has nobody to ask, so a call that needs approval is refused, and the refusal is reported:

```bash
seawall -p "fix the failing test"                              # reads and searches only
seawall -p "fix the failing test" --allowed-tools bash,edit_file     # these tools run without asking
seawall -p "fix the failing test" --permission-mode full_auto        # nothing asks; critical calls still refused
```

Refused calls show up on stderr (`text`), as `permission_denials` in the final object (`json`), and as `"denied": true` on `tool_completed` events (`stream-json`). The model is told why the call did not run and what the user can change, so it stops retrying.

`--allowed-tools` and `--disallowed-tools` take comma- or space-separated tool names, matched without regard to case, and add to what the settings file already lists. A name that matches no tool is reported on startup instead of silently doing nothing.

### Sub-agents

A sub-agent runs in its own process and cannot ask anyone. It inherits the parent's permission mode and tool lists, and refuses what would need approval. In `default` mode a sub-agent can read and search but not write; to let sub-agents edit, run the parent in `full_auto` or pass `--allowed-tools`. A sub-agent never gets more latitude than its parent.

## The audit log

Each session writes one file, `<data dir>/audit/<session id>.jsonl` (mode 600), one JSON object per line:

| Record | When |
| --- | --- |
| `session.start` / `session.end` | the session opens and closes; start records the cwd, mode and tool lists |
| `tool.decision` | before a tool runs: the redacted input, risk label, `allow` or `deny`, who decided (`policy`, `approval`, `hook`) and why |
| `approval.requested` / `approval.resolved` | around an approval: the question, then the outcome, who answered and how long it took |
| `tool.result` | after a tool runs: ok or error, duration, size and a short digest of the output |

What is not in the log: file contents (a `write_file` is logged as a length and a digest), full command output, and anything that looks like a credential. Tokens, `Bearer` headers, private keys, URL passwords and `NAME=value` secrets are replaced with `[REDACTED]`, in the log and in approval prompts. Redaction is pattern-based and will miss secrets in unusual shapes.

Read it with:

```bash
seawall audit list                                   # sessions, with the result of the chain check
seawall audit show <session> --decision deny         # what was refused
seawall audit show <session> --min-risk high --json
seawall audit verify <session>                       # exits 1 if the log was altered
```

### What the chain proves

Every record contains the hash of the record before it. Editing, deleting, inserting or reordering a record breaks the chain at a known line, and `seawall audit verify` reports it.

* **Plain chain (default).** SHA-256. Anyone who can write the file can rewrite the whole chain, so this catches accidents and careless edits. It cannot see records cut off the end unless the final hash was kept somewhere else: print it with `seawall audit verify`, store it, and pass it back with `--head`.
* **Signed chain.** Set an HMAC key in the environment variable named by `audit.key_env` (default `SEAWALL_AUDIT_KEY`) before the session. Forging a record then needs the key. Keep the key where the agent process cannot read it, or it proves nothing; verify with the same variable set. A signed log is refused if verified without the key, and an unsigned log is refused if verified *with* a key, so a forger cannot pass an unsigned rewrite off as the original.
* `audit.fail_closed: true` refuses to run a tool whose decision cannot be written. The default is to warn once and carry on.

Settings, all under `audit` in `settings.json`: `enabled`, `directory`, `key_env`, `fail_closed`, `max_field_chars`.
