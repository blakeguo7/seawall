# Tracing

The audit log says what was decided. A trace says where the time and the money went. Every session writes one file with a **span** for each step of each run: the run, each turn of the loop, each model call, each tool call, each wait for an approval, each compaction. Spans point at their parent, so a run reads back as a tree.

```text
$ seawall trace show 2745143edb4a
run 1 of 1  trace d121d684  started 2026-10-08T17:37:32.120+00:00

run  1.37s  completed  2 turns  437 tokens  $0.0003
├─ turn 1  770ms
│  ├─ model.call  690ms  deepseek-flash  in 215 out 55  $0.0002
│  └─ tool.call bash  21ms  ok  ran 18ms  risk low
└─ turn 2  594ms
   └─ model.call  540ms  deepseek-flash  in 157 out 10  first text 494ms  $0.0001

where the time went (1.37s):
  model calls      1.23s   90%  2 calls, 437 tokens, $0.0003
  tools             21ms    2%  1 calls
  approval wait      0ms    0%
  compaction         0ms    0%
  other            115ms    8%
slowest: model.call 690ms (turn 1), model.call 540ms (turn 2), tool.call bash 21ms (turn 1)
```

## Commands

| Command | What it does |
| --- | --- |
| `seawall trace list [-n N]` | Sessions with a trace, newest first: runs, total time, cost, how the last run ended. |
| `seawall trace show SESSION` | The last run as a tree, then where its time went. `SESSION` is an id, a prefix of one, or a file path. |
| `seawall trace show SESSION --run 2` / `--all` | Another run of the session, or every run. |
| `seawall trace show SESSION --json` | The raw spans, one JSON object per line, for your own tools. |

The five parts of "where the time went" add up to the run. A tool call that waited for approval counts the wait as approval, not as tool time, and tool calls that ran side by side are counted once. A model call made inside a compaction counts as compaction.

## What is recorded

| Span | Hangs under | Attributes |
| --- | --- | --- |
| `run` | nothing (it starts a trace) | `model`, `max_turns`; at the end `stop_reason`, `turns`, `detail`, and the tokens and cost **of this run alone** |
| `turn` | `run` | `index` |
| `model.call` | the turn, or the compaction, that made it | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd` (`null` when the model has no price), `first_text_ms`, `retries`, `tool_calls`, `api_stop_reason`, `error` |
| `tool.call` | the turn that asked for it | `tool`, `call_id`, `input` (redacted like an audit record), `risk`, `needs_approval`, `exec_ms` (the tool itself, without hooks and approval), `output_chars`, `denied_by`, `error` |
| `approval.wait` | its `tool.call` | `tool`, `risk`, `outcome`, `decided_by` |
| `compaction` | the turn | `trigger`, `forced`, `compacted`, `phases` |

`status` is `ok`, `error`, `denied` (a tool call that policy, a hook or an approver refused), `stopped` (a run cut short by a limit or the loop guard; `stop_reason` says which) or `cancelled`.

A compaction check happens at the start of every turn and nearly always finds nothing to do, so a check that did nothing leaves no span. `first_text_ms` is missing when the reply had no text, which is the case for a reply that only asks for tools.

Model calls made outside a run, such as memory extraction after the answer, are spans of their own with no parent; `trace show` lists them under "outside any run".

## In evaluations

An eval trial runs the agent under a throwaway `HOME`, so its trace lands there. After the agent exits, the harness reads it and stores a few counts and the time split with the trial's result, and uses them to say why a failed trial failed (`seawall eval explain`). See [Why a trial failed](evals.md#why-a-trial-failed).

## Settings

```json
{
  "trace": {
    "enabled": true,
    "directory": "",
    "capture_content": false,
    "max_field_chars": 1000
  }
}
```

Files go to `<data dir>/traces/<session id>.jsonl`, created `0600`. Nothing rotates them. With `capture_content` off a span holds timings, counts, costs and statuses, and no message or output text. Turn it on to keep a short preview (300 characters) of each model reply and tool output, which is what you want when working out why a run went wrong. Every string attribute is passed through the same credential redaction as the audit log and cut to `max_field_chars`, but a preview is still text from your session: treat the file accordingly.

## What it is not

- **Not tamper-evident.** The audit log is hash-chained and can be signed; a trace is a plain file for diagnosis, not evidence.
- **Not complete after a crash.** A span is written when it ends, so one that was running when the process died is missing. The run that was cut short shows up as the parent whose children are there but which itself is not.
- **Not joined across sub-agents.** A sub-agent is its own process with its own session and its own trace file.
- **Not exported anywhere.** The field names (`trace`, `span`, `parent`, `name`, `start`, `status`, `attrs`) follow OpenTelemetry, so a converter to OTLP is a mapping, but there is none yet.
- **A write that fails is a warning, once.** The agent carries on untraced. It never stops a run.
