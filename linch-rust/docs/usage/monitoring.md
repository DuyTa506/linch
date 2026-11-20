# Monitoring

Watch what your agents do and what they cost: traces, cost, evals and run
reports.

[← Usage guide](./README.md)

Four tools cover it. Logs and traces go to your collector through
OpenTelemetry. Cost is on every event. An eval runs a list of cases and scores
them. A run report folds one stored run into tool calls, timings and totals.

---

## What is traced

The client turns each run's event stream into OpenTelemetry spans and metrics
with the names Python Linch uses: one span per run, per model call and per
tool call, and metrics for tokens, cost and durations. Your host owns the SDK
and the exporter (Langfuse, Tempo, Datadog, any OTLP collector). Nothing is
built unless you install the `otel` extra and set a provider, so an untraced
agent pays nothing.

The setup, the span and metric names and how to correlate logs are in
[Observability](./observability.md).

---

## How cost is computed

Each model call that reports usage is priced from the call's four token
buckets, each at its own rate per million tokens:

```text
cost = input * input_rate + output * output_rate
     + cache_read * cache_read_rate + cache_creation * cache_creation_rate
```

`input_tokens` is the uncached input only: an adapter subtracts the cache
reads and writes its vendor counts inside the prompt. The result is on the
`usage` event (`cost_usd`, `cumulative_cost_usd`) and on the `result` event
(`total_cost_usd`). Pricing never changes a request or any other event field.

**The table.** The built-in table is Python Linch's and holds six Anthropic
ids, at the rates published on 2026-06-08 (https://www.anthropic.com/pricing),
with cache writes at the 5-minute tier:

| Model id | Input | Output | Cache read | Cache write |
|---|---:|---:|---:|---:|
| `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` | 5.00 | 25.00 | 0.50 | 6.25 |
| `claude-sonnet-4-6` | 3.00 | 15.00 | 0.30 | 3.75 |
| `claude-haiku-4-5`, `claude-haiku-4-5-20251001` | 1.00 | 5.00 | 0.10 | 1.25 |

USD per million tokens. A model is matched on its exact id.

**By model and by subagent.** The `result` event splits its usage two ways,
and each split sums to what it splits:

- `usage_by_model` (model id to the four usage buckets) sums to
  `total_usage`. The key is the model that served the call
  (`actual_model`; the run's current model when the provider named none), so
  a fallback or a compaction summary on another model shows as its own
  entry. The `usage` event carries the running split as `cumulative_by_model`.
- `subagent_usage` (child run id to `{subagent_type, total_usage,
  usage_complete, usage_by_model, total_cost_usd}`) holds what each subagent
  child run the run started spent, as the child's own `result` reports it.
  `total_usage` counts the run's own calls only, so the whole tree is
  `total_usage` plus each entry's `total_usage`, and the tree by model is
  `usage_by_model` plus each entry's `usage_by_model`. A child that was
  never started or has not ended is left out.

Both are left out of the event when empty, so a run with one model and no
subagent reports what it always did. A run ended by `Agent.fail` reports
`usage_by_model` from its last checkpoint and no `subagent_usage`. Python:
`ResultEvent.usage_by_model`, `ResultEvent.subagent_usage` (`SubagentUsage`)
and `UsageEvent.cumulative_by_model`.

**Every other model costs `None`**, never a silent zero and never a guess:
OpenAI, Gemini and DeepSeek models, and every model a local server (vLLM,
SGLang, llama.cpp) serves. Python Linch has no rates for them either. A run
with several models adds only its priced calls; a run with none priced has
`total_cost_usd: None`. An agent with a USD `budget` and a model without a price
does not open (`-32011`, naming the model): price the model with `pricing`, or
use a token cap.

**Price a model yourself** with `pricing` in the agent config. It is per
agent, by exact model id, and replaces the table for that model or adds one it
lacks:

```python
config: AgentConfig = {
    "namespace": "demo",
    "spec": {"model": "Qwen/Qwen3-32B"},
    "provider": {"id": "vllm", "base_url": "http://localhost:8000/v1"},
    "pricing": {
        # USD per million tokens. A local server you own can cost 0.
        "Qwen/Qwen3-32B": {"input": 0.0, "output": 0.0},
        # Or an amortised rate; cache rates default to `input`.
        "my-finetune": {"input": 0.2, "output": 0.6, "cache_read": 0.05},
    },
}
```

**DeepSeek** has no row in the table: its API price depends on the hour
(off-peak is half the peak rate; peak is 01:00 to 04:00 and 06:00 to 10:00 UTC
on weekdays, other than Chinese public holidays), so one built-in rate would be
wrong half the time. Set the rates you pay, with the cache-hit rate as
`cache_read`. The rates below are the peak ones read on 2026-11-17 from
https://api-docs.deepseek.com/quick_start/pricing, in USD per million tokens;
check the page before relying on them:

```python
"pricing": {
    "deepseek-flash": {"input": 0.3, "output": 1.2, "cache_read": 0.006},
    "deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cache_read": 0.044},
},
```

`input` and `output` are required; `cache_read` and `cache_creation` default
to `input`, so a cached token never costs less than you said. Rates must be
finite and not negative; anything else is refused when the agent opens.

**Cache usage from local servers.** `cache_read_tokens` comes from the fields
each server reports: `prompt_tokens_details.cached_tokens` on vLLM (and
`created_cache_tokens` as cache writes), SGLang and llama.cpp; SGLang's
top-level `cached_tokens` when it sends no details; and for llama.cpp the
other names Python reads (`cache_read_tokens`, `prompt_cache_hit_tokens`) and
`timings.cache_n`. vLLM and SGLang report none unless started with
`--enable-prompt-tokens-details` or `--enable-cache-report`: then
`cache_read_tokens` is 0 because it is unknown, not because nothing was
cached. On SGLang 0.5.23 a server started without `--enable-cache-report` sent no `prompt_tokens_details` and linch reported `cache_read_tokens` 0 on every run, with no error or warning
([running linch against an SGLang server](providers.md#running-linch-against-an-sglang-server)).

---

## Run an eval

An eval runs cases on an open agent, one after another, each on a fresh
session, and scores each run.

```python
import asyncio

from linch_rust import Server
from linch_rust.scripted import MODEL, text_turn

CONFIG = {
    "namespace": "demo",
    "spec": {"model": MODEL},
    "provider": {"id": "scripted", "turns": [text_turn("Paris."), text_turn("Munich.")]},
    "pricing": {MODEL: {"input": 3.0, "output": 15.0}},
}


async def main() -> None:
    async with Server() as server:
        agent = await server.open_agent(CONFIG)
        report = await agent.eval(
            [
                {"prompt": "Capital of France?", "expected": "Paris"},
                {"prompt": "Capital of Germany?", "expected": "Berlin"},
            ],
            [
                {"type": "text_contains"},
                {"type": "cost_under", "budget_usd": 0.01},
                {"type": "run_completed"},
            ],
        )
        print(report["passed"], "of", report["total"], report["total_cost_usd"])
        print(report["markdown"])


asyncio.run(main())
```

A case is `{prompt, expected?, metadata?}`; the prompt is not empty. A tenant's
`eval.run` takes at most 256 cases (`-32602` beyond that, before any runs); a
local connection has no cap. The runs are claimed under the server's id like any
run it drives. A case
passes when its run records no error and no scorer returns `False`. A scorer
returns `None` (unknown) when the run lacks the data it reads, and unknown does
not fail a case.

**Built-in scorers** run in the server and are dicts with a `type`:

| `type` | Fields | Passes when |
|---|---|---|
| `text_contains` | `substring` (default `{expected}`) | the output contains it, ignoring case; `{expected}` is the case's expected answer |
| `tool_called` | `tool` | the run started a call of that tool |
| `schema_valid` | `schema` | the output is JSON the schema accepts |
| `cost_under` | `budget_usd` | the run's `total_cost_usd` is strictly below it; unknown without a price |
| `run_completed` | none | the run ended with a successful result |
| `recovery_succeeded` | `tool?` | a failed tool call was followed by a successful one |
| `context_selected_tool` | `tool` | a `context_build` event offered the tool |
| `context_not_trimmed` | none | no `context_build` event reports trimming |
| `context_metadata_contains` | `key`, `expected?` | a `context_build` event's metadata holds the key (and value) |

An unknown type or field, or an empty string where one is required, is a
`ProtocolError` and nothing runs.

**Custom scorers** are Python callables, sync or async, that take the case's
result and return `True`, `False` or `None`. They run in your process after
the cases, so they need no callback and cannot see the live run; read events
with `agent.events(result["run_id"])` or ask for them with
`include_events=True`. The client adds their verdicts under the function's
`__name__`, recomputes `passed` and `pass_rate`, and drops `markdown`, which
the server wrote without them.

```python
def short(result):
    return len(result["output"]) < 200

report = await agent.eval(cases, [{"type": "run_completed"}, short])
```

**The report** (`EvalReport`) holds `total`, `passed`, `pass_rate`,
`total_cost_usd` (the sum of the priced cases, `None` when none is priced),
`usage` (the four token buckets summed), `duration_ms` and `cases`. Each case
result has its `case`, `output`, `passed`, `scores` (`[{name, verdict}]`),
`error`, `tool_calls`, `total_cost_usd`, `usage`, `duration_ms`, `event_count`
and `run_id`. The `markdown` field is a table with one row per case. Events
are left out unless `include_events=True`.

**Behaviour to know.**

- `eval` returns when every case has run; there is no progress stream and no
  way to cancel one from the client except closing the connection, which stops
  it where it stands.
- Each case is a stored run, so `agent.report(case["run_id"])` and
  `agent.events(...)` work on it.
- An eval holds one of a remote tenant's run places while it runs
  (`LimitReached`), and `agent.close` is refused (`RunBusy`) until it ends.
- A case that suspends for approval or input is left suspended and reported
  without a result: its cost and duration are `None`. Evaluate agents whose
  permission mode does not ask, or whose tools are approved by a callback.
- With scripted turns the cases consume turns in order.

---

## Read a run report

`agent.report(run_id)` folds one stored run, finished or not, into a report.

```python
one = await agent.report(run_id)
report = one["report"]
print(report["status"], report["final"]["total_cost_usd"])
for call in report["tool_calls"]:
    print(call["tool_name"], call["duration_ms"], "failed" if call["is_error"] else "ok")
print(one["markdown"])
```

The `RunReport` fields:

| Field | What it holds |
|---|---|
| `run_id`, `session_id`, `status`, `phase` | The run and where it stands: `running`, `awaiting_approval`, `awaiting_input`, `completed`, `error`, `aborted` or `interrupted` |
| `event_count` | Events read |
| `tool_calls` | Every ended call in end order: `tool_name`, `input`, `summary`, `result`, `is_error`, `duration_ms` |
| `permission_requests` | Calls that asked for permission |
| `errors` | Every `error` event's payload |
| `usage` | The last `usage` event |
| `final` | The `result` event: `subtype`, `total_usage`, `total_cost_usd`, `duration_ms`, `final_text` |
| `summary` | `event_counts`; `usage` with `total_tokens` and `cache_read_ratio`; `tools` (total, average and maximum duration, the slowest and the failing calls); `context`, `recovery`, `prompt_cache` and `risk` figures |
| `long_run` | Context growth, quality and resume signals |
| `timeline` | Every event; empty unless `timeline=True` |

A report is built to fit 8 MiB, well within one message. Three steps, in this order, while it is longer:

1. A string longer than 64 KiB (a long answer or tool result) is cut and ends in `... [truncated: N more bytes]`.
2. Past 8 MiB the timeline loses its last events.
3. Past 8 MiB still, the text of each tool call (its input and its result) is cut to 8 KiB, then to 1 KiB.

When anything was cut, the answer has `truncated`: `{"strings": n, "timeline_omitted": m, "tool_call_text": k}`, where `timeline_omitted` is present only if events were dropped and `tool_call_text` (the number of tool calls whose text was shortened) only if step 3 ran. Read the full text with `agent.events`. A report that is still over 8 MiB with every tool call at 1 KiB (many thousands of tool calls) is refused with `-32603`; read such a run with `agent.events`.

The shape of the stream does not matter: a run with no `result` event has
`final: None` and the status it had. An unknown run is `RunNotFound`.

To find the slow or failing tool of a run, read `summary["tools"]`. To compare
cost across runs, read `final["total_cost_usd"]` and `final["total_usage"]`,
or let an eval do it over a fixed set of cases.

---

## Related pages

- [Observability](./observability.md): spans, metrics and log correlation.
- [Events](./events.md): the usage and result fields cost is on.
- [Providers](./providers.md): the provider ids and local servers.
- [Runs](./runs.md): the stored runs a report reads.
