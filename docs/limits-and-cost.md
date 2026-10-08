# Cost, limits and the loop guard

A coding agent that runs unattended can burn money in two ways: by working too long, and by repeating itself. Seawall counts what each session spends, can stop a run when it reaches a limit, and stops an agent that is going in circles.

## What is counted

Every model call the engine makes goes through one metered client: the turns of the agent loop, the summaries written when the conversation is compacted, and memory extraction. Calls made by prompt hooks and auto-dream are not counted.

Counted per call: input tokens, output tokens, and prompt-cache reads and writes when the provider reports them. Providers that include cache hits in the prompt count (OpenAI, DeepSeek) are normalized so that `input_tokens` is the part billed at the full rate.

`/cost` shows the totals for the session, the estimated cost, and how much of each limit is used. Headless runs put the same numbers in their output (see below).

## Prices

Cost is tokens times price, and **the price table is configuration, not knowledge**. A built-in table in [`engine/pricing.py`](../src/seawall/engine/pricing.py) has approximate list prices for common Claude and OpenAI models, matched by longest name prefix (`claude-sonnet-4-6` matches `claude-sonnet-4`). It is a starting point: providers change prices, resellers charge differently, and newer models are not in it. **A model with no price is reported as unpriced; the harness does not guess.** Its tokens are still counted, and `/cost` and the run summary say which models are missing from the dollar figure.

Set or override prices in `settings.json`, in US dollars per million tokens. Entries take precedence over the built-in table and match by name prefix:

```json
{
  "pricing": {
    "deepseek-chat": {"input": 0.27, "output": 1.10, "cache_read": 0.07},
    "my-gateway-model": {"input": 2.0, "output": 8.0}
  }
}
```

`cache_write` defaults to 1.25 times `input` and `cache_read` to 0.1 times `input` when omitted. Use your provider's current price list; the numbers above are examples.

## Limits

| Flag | Setting | Counts | Stops with |
| --- | --- | --- | --- |
| `--max-budget-usd` | `limits.max_budget_usd` | estimated cost of the whole session | `budget_exceeded` |
| `--max-total-tokens` | `limits.max_total_tokens` | all tokens of the whole session, cache traffic included | `token_limit` |
| `--max-seconds` | `limits.max_seconds` | wall-clock time of one prompt | `time_limit` |
| `--max-turns` | `max_turns` | model calls for one prompt | `max_turns` |

Limits are checked before every model call, so a run overshoots by at most the one call and tool batch in flight when the limit was crossed. They are not a hard ceiling on a single call: a long output can cross the line by itself.

A dollar budget needs a price for the model in use. If there is none, the run stops before any call with `no_price` and says what to add; use `--max-total-tokens` instead, or add the price. A limit that cannot be measured is refused, not ignored.

When a run stops, the conversation is left valid (every tool call has its result), so raising the limit and sending another prompt continues from where it stopped.

## The loop guard

An agent can get stuck repeating the same failing command. The guard watches tool calls and acts on two patterns:

* **The same call with the same result**, seen 3 times within the last 12 calls: a warning is appended to the tool result, telling the model that repeating the call changes nothing. At 5 times the run stops with `loop_detected`. The same call with a *different* result counts as progress (an edit-and-retest cycle is fine), and alternating between two repeated calls is caught too.
* **Failing calls in a row**: a warning after 4, a stop after 8.

This also bounds what a model that keeps retrying a refused call can cost. Thresholds live under `limits.loop_guard` (`enabled`, `warn_after`, `stop_after`, `window`, `warn_errors_after`, `stop_errors_after`).

## How a run reports it

Every run ends with a `RunFinished` event carrying the stop reason, turns, session usage, estimated cost and the unpriced models. In `seawall -p`:

```bash
seawall -p "fix the failing test" --permission-mode full_auto --max-budget-usd 2 --output-format json
```

```json
{"type": "result", "text": "...", "stop_reason": "budget_exceeded", "detail": "Budget reached: $2.1480 of $2.00.",
 "turns": 14, "duration_seconds": 93.2,
 "usage": {"input_tokens": 612000, "output_tokens": 18000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
 "cost_usd": 2.148, "unpriced_models": [], "permission_denials": []}
```

`cost_usd` is `null` when some model used has no price. With `stream-json` the same fields arrive in a final `run_finished` object.

| Exit code | Meaning |
| --- | --- |
| 0 | the model finished on its own (`completed`) |
| 1 | an API error ended the run (`error`) |
| 2 | the run was cut short: `max_turns`, `budget_exceeded`, `token_limit`, `time_limit`, `loop_detected` or `no_price` |

Every run is also written to the audit log as a `run.finished` record with the same figures.
