# Events

[← Usage guide](./README.md)

Everything the agent loop tells you arrives as a typed **event**.
`session.run(prompt)` returns an async iterator of events; you stream them to
your UI, your logs, or your metrics as they happen. This page covers the event
taxonomy, the cost fields events carry, and the offline tooling (run reports and
eval scorers) you use to debug or grade a run after the fact.

---

## Event types

Match on `event.type` to handle each kind. The loop yields events in real time
as the turn progresses:

```python
async for event in session.run("hello"):
    match event.type:
        case "system":    # run started — model, tools, cwd
            pass
        case "user":      # user message appended
            pass
        case "assistant": # full assistant turn (final)
            pass
        case "partial_assistant":  # streaming text/thinking delta
            pass
        case "tool_call_start":   # tool about to run
            pass
        case "tool_call_end":     # tool finished, has .result
            pass
        case "tool_progress":     # transient progress from an in-flight tool
            pass
        case "permission_request": # user approval needed (mode="default")
            pass
        case "usage":     # primary or auxiliary token counts
            pass
        case "budget":    # RunBudget warning (90%) or exhaustion
            pass
        case "result":    # .subtype: "success", "error", "aborted", "interrupted"
            pass
        case "error":     # provider/tool error details
            pass
        case "compaction": # context was summarised (or micro-compacted)
            pass
        case "workflow":  # workflow engine progress / journal records
            pass
```

A few lifecycle rules worth internalizing:

- **`system` is first, `result` is always last.** Every successful or failed run
  terminates with exactly one `ResultEvent`. Treat it as the signal to stop
  consuming and read the outcome.
- **`partial_assistant` only appears when `include_partial_messages=True`.**
  These are the streaming text/thinking deltas (see
  [Providers → Reading thinking events](./providers.md#reading-thinking-events)).
  Without that flag you still get the full `assistant` event at the end of each
  turn.
- **`tool_call_end` carries rendered result text** on `.result`; the rich
  `ToolResult` fields (summary, metadata, citations) are on `.tool_result`,
  even when the model only saw a truncated/offloaded preview.
- **`tool_call_end.tool_output` carries Canonical Tool Output V2** for tools
  declaring `output_schema`: the validated JSON value or structured error and
  JSON-reference attachments remain available beside the legacy projections.
- **`tool_progress` is observational only.** A tool may call
  `ctx.report_progress("scanning", {"completed": 3})`; Linch emits a
  `ToolProgressEvent` for consumers, but never appends it to provider history,
  the durable run event log, or changes the final `ToolResult`. Progress is
  best-effort and may be ignored by consumers.
- **`permission_request` pauses the loop** when running in `mode="default"`; the
  loop resumes once you respond. With `mode="skip-dangerous"` you never see it.
- **`budget` fires on a `RunBudget`** — once as a warning at 90%, then on
  exhaustion. See [Agent](./agent.md) for configuring `RunBudget`.

Check `event.subtype` on the terminal `ResultEvent` and read `event.final_text`
(or `event.structured_output` when using an `OutputSchema`):

```python
async for event in session.run("hello"):
    ...
# ResultEvent is always the last event. Check `event.subtype` and
# `event.final_text` (or `event.structured_output` when using an OutputSchema).
```

`event.subtype` is one of `"success"`, `"error"`, `"aborted"`, or
`"interrupted"` — branch on it to decide whether to surface the answer, an
error, or a cancellation/interruption.

The same event stream is mirrored to hooks at the `on_event_emit` chokepoint, so
anything you can do in this loop you can also do in a reusable hook — see
[Hooks](./hooks.md).

## Model-call accounting

In a normal run, Linch accounts for every physical provider delivery that reaches a normalized
`message_end` with valid `Usage`, including the auxiliary provider delivery
used by a model-backed compaction. A retry or fallback gets a fresh physical
delivery ID and is accounted separately even when it belongs to the same
logical operation. Valid usage is independent of whether the output is usable:
an empty, tool-using, non-text, or oversized compaction result is rejected but
its reported usage is still charged once.

`ModelCallEvent` is the public, immutable envelope for the private accounting
journal. It is not yielded by `session.run()`. With a configured `RunStore`,
use `load_events()` and the type guard to inspect persisted accounting records:

```python
from linch import ModelCallEvent, is_model_call_event

# run_store is the configured RunStore; run_id comes from SystemEvent.run_id.
cursor = 0
for row in await run_store.load_events(run_id, after_seq=cursor):
    event = row.event
    if is_model_call_event(event):
        assert isinstance(event, ModelCallEvent)
        print(row.seq, event.record_kind, event.run_id)
    cursor = row.seq
```

The envelope carries a run ID, one of `intent`, `dispatch_started`, `receipt`,
`abandoned`, or `projection`, and a detached JSON-safe record. Receipts,
intents, and gateway details are private journal records; they are not extra
names in `linch.__all__`. Model-call records are journal-only and are not
yielded by the normal `session.run()` stream. A
compaction receipt is surfaced through an additive `UsageEvent` instead:

```python
if event.type == "usage" and event.source == "compaction":
    print(event.usage, event.call_id, event.logical_call_id, event.model)
```

The existing `UsageEvent` ordering and legacy fields remain compatible; the
accounting metadata is additive. Its `usage` is
the physical delivery's usage and `cumulative` is the run's cumulative known
usage across primary and auxiliary deliveries. `ResultEvent.total_usage` and
`RunReport` use the same cumulative lower-bound total. When a dispatched call
is cancelled, fails, or ends before valid final usage, the total is not
invented as zero: the terminal result sets `usage_complete=False`.

---

## Cost fields

Usage and result events include optional USD cost fields when the model exists
in `linch.pricing`'s table:

```python
async for event in session.run("Summarize this thread."):
    if event.type == "usage":
        print(event.cost_usd, event.cumulative_cost_usd)
    elif event.type == "result":
        print(event.total_cost_usd)
```

`usage` events report the cost of the physical delivery (`cost_usd`) and the
running known total so far (`cumulative_cost_usd`); the terminal `result` event
reports the run total (`total_cost_usd`). The cumulative token count includes a
compaction delivery before the main turn that follows it.

**Unknown model IDs report `None`** for every cost field rather than charging a
synthetic price — this is deliberate so a private or self-hosted model never
shows a misleading number. Known token usage is retained, but the aggregate
cost is partial and a USD cap cannot enforce an unknown price. To attribute cost
for such models, pass a custom table:

```python
# linch.pricing.cost_usd(usage, model, table=...) with a custom table
# for private or self-hosted models.
```

This matters for budgets too: with an unpriced model, only the token limit of a
`RunBudget` can bind for that delivery (the cost limit cannot trip on an
unknown price). See
[Agent](./agent.md) for budget configuration.

### Budget fences and compaction

The compaction delivery settles before its summary is applied, hooks continue,
or the next provider request is constructed. The loop rechecks admission after
that settlement, so a summary can exhaust a token/USD cap and prevent the main
turn from dispatching. If a dispatched delivery has unknown usage, a capped run
fails closed before another model call rather than claiming that the cap is
enforced. An uncapped run may continue, but its result remains an explicit
lower bound (`usage_complete=False`).

The private receipt ledger shares a live `RunBudget` owner across sessions and
subagents in the same in-process tree, so a sibling cannot continue past an
unknown-use fence or replay an already charged delivery. This is not a
distributed-tree guarantee and does not promise exactly-once billing at an
external provider or a reservation against concurrent admissions.

## OpenTelemetry traces and metrics

The event stream remains the application-facing way to render progress and
results. Operational traces and metrics are emitted at the I/O source through
`opentelemetry-api`; without an SDK provider they are no-ops. Inject a host's
tracer and meter providers through the agent `Context` (or inject direct
tracer/meter objects) so different agents can use different exporters:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from linch import Agent, Context

host_context = Context()
host_context.register("tracer_provider", TracerProvider())
host_context.register("meter_provider", MeterProvider())
agent = Agent(model="gpt-5", provider=my_provider, context=host_context)
```

The source instrumentation creates `invoke_agent`, `step`, `chat`,
`execute_tool`, and compaction spans. GenAI attributes include operation,
provider, model, finish reasons, token/cache usage, conversation id, tool name
and call id; Linch adds run and turn identifiers. It records
`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, and
`linch.store.duration` (tagged with `op`) metrics. Content fields are omitted
unless `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` is set. Use
an OTLP exporter for Langfuse, Tempo, Datadog, or another collector; no custom
telemetry hook is required.

The run boundary events expose the same correlation value as `trace_id`. With a
valid active OpenTelemetry context this is the 32-character trace ID; without
one, Linch reuses the durable `run_id` so the fallback costs no extra trace
setup. The creation/origin ID is stored in `RunRecord.meta["trace_id"]` and is
read-only on each tool's `ToolContext.trace_id`. A resumed attempt follows its
caller's active trace, or the same durable run ID when no active trace exists.
Hosts can add it to ordinary log records without installing global logging
state:

```python
import logging

from linch import TraceIdFilter

handler = logging.StreamHandler()
handler.addFilter(TraceIdFilter())
handler.setFormatter(logging.Formatter("%(trace_id)s %(message)s"))
logging.getLogger("my-service").addHandler(handler)
```

An explicit `extra={"trace_id": ...}` wins, followed by the active OTel span,
`record.run_id`, and an optional filter fallback. A tool can pass its captured
value explicitly: `logger.info("search complete", extra={"trace_id": ctx.trace_id})`.
See the generated lifecycle
catalog in [the architecture guide](../architecture/events.md#typed-lifecycle-catalog)
for each point's payload and dispatch mode. Hook points are owned by the
ordered `HookDispatcher`; tool pipeline points are waterfalls, while
`turn-stopping` is the serial veto seam.

---

## Backpressure

`session.run(prompt)` returns a plain `AsyncIterator[Event]` — an async generator
that `yield`s each event and **suspends at the yield until you pull the next one**.
The chain (`run_loop` → `session.run`) is generators all the way down; there is no
unbounded internal queue draining events away from your consumer.

The practical guarantee: **a slow consumer throttles the whole producer.** While
you are not pulling, provider streaming and tool execution do not race ahead — the
loop is parked at the last `yield`. So you can safely do slow per-event work
(write to a socket, render a UI frame, persist to a DB) without the run buffering
unboundedly in memory:

```python
async for event in session.run(prompt):
    await slow_websocket.send(serialize(event))  # backpressure flows upstream
```

Consequences to design for:

- **Always drain the iterator.** Abandoning it mid-stream leaves the run parked.
  Either consume to the terminal `ResultEvent`, or call `session.abort()` (which
  cancels in-flight provider/tool work and any background tasks) and then let the
  generator finish. Breaking out of the `async for` triggers the generator's
  `aclose()`, running the `finally` that clears the active-run flag.
- **One active run per session.** A second `session.run()` while one is live
  raises `ConfigError` — fan out with separate sessions (or subagents) instead.
- **Background work is the explicit exception.** A `run_in_background=True` tool or
  subagent detaches onto its own task and delivers completion as a drained
  `<task-notification>` on a later in-process turn — that path intentionally does
  *not* block the foreground stream. With a `RunStore`, status is best-effort
  audit telemetry on the origin run; the result notification and active task are
  not reconstructed after restart. See [Tools](./tools.md) and
  [deep-agent](./deep-agent.md).

---

## Run reports

Use run reports when you need a compact debugging export for a finished or
in-progress run. Reports are built from typed events and, when available,
`RunStore` checkpoints. They are a pure **read model** — building one never
mutates session state.

Load a report from a durable run store by `run_id`:

```python
from linch.ext.reports import load_run_report

report = await load_run_report(agent.run_store, run_id)
print(report.to_markdown())

payload = report.to_dict()
print(payload["summary"])
print(payload["context_builds"])
print(payload["permission_requests"])
print(payload["tool_calls"])
print(payload["loop_guards"])
print(payload["long_run"])
```

For transient runs where you already collected events, build a report directly
from the list you accumulated — no store required:

```python
from linch.ext.reports import build_run_report

events = []
async for event in session.run("Summarize the policy change."):
    events.append(event)

report = build_run_report(events)
```

Use `to_markdown()` for a human-readable dump (logs, PR comments) and
`to_dict()` when you want to inspect or serialize specific facets.

For a quick local report from a durable Postgres run store, use the bundled script:

```bash
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" <run_id>
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" --format markdown <run_id>
PYTHONPATH=src python scripts/run_report.py --dsn "$LINCH_POSTGRES_DSN" --format json <run_id>
```

The script accepts `--dsn` or defaults to `LINCH_POSTGRES_DSN`; it requires one
of these to be set. See [local Postgres setup](./quickstart.md#5-keep-state-when-the-process-restarts).

`payload["summary"]` is the compact observability surface: event counts,
duration, total tokens/cost, cache-read ratio, tool latency and error rate,
context utilization, recovery counters for compaction, model fallback,
verifier/hook retry, result offload, and risk counters such as permission
requests, loop guards, errors, and recovery hints.

`payload["long_run"]` summarizes signals that matter in long-horizon sessions:
context build counts, trimmed context builds, max used context tokens, selected
tool counts, failed tool calls, recovery hints, completion status, total cost,
and checkpoint phase.

---

## Long-run eval scorers

The run report tells you what *one* run did; the **eval harness** grades many
runs deterministically offline. Its scorers cover context and memory behavior in
addition to the usual text, schema, tool, and cost checks — each returns
`True`/`False`/`None`:

```python
from linch.ext.evals import (
    context_metadata_contains,
    context_not_trimmed,
    context_selected_tool,
    cost_under,
    recovery_succeeded,
    run_completed,
    run_eval,
)

result = await run_eval(
    agent,
    cases,
    scorers=[
        context_selected_tool("SearchPolicy"),
        context_not_trimmed(),
        recovery_succeeded(),
        cost_under(0.05),
        run_completed(),
    ],
)
```

`run_eval` runs the agent over a list of `EvalCase`s and returns a result with
per-scorer pass/fail/`None`. A `None` means "not applicable / not observed" —
distinct from a hard fail — so a scorer that never saw the relevant signal does
not silently count as a pass. Pair this with a `ScriptedProvider` to grade
behavior without spending a live model call. For suite files, target
comparisons, and CI exit codes, see [Evals and benchmarks](./evals.md).

---

## Trace context across host jobs

`invoke_agent` uses the caller's current span as its parent. Foreground and
background subagents inherit that context through their tool tasks. A run's
trace identifier is the OTel trace ID; `linch.run_id` remains its durable run ID.
When a host dispatches work to another process, carry W3C `traceparent` in the
job payload using OTel propagation:

```python
from opentelemetry import context, propagate

# Producer, inside the host request span:
carrier = {}
propagate.inject(carrier)
job = {"run_id": run_id, "trace_context": carrier}

# Consumer, after loading the session for this job:
token = context.attach(propagate.extract(job["trace_context"]))
try:
    async for event in session.resume(job["run_id"]):
        ...
finally:
    context.detach(token)
```

Install `opentelemetry-sdk` and `opentelemetry-exporter-otlp` in the host for
OTLP export. Configure SDK trace processors and metric readers with OTLP
exporters, then register their providers in `Context` as shown in
[the hooks guide](./hooks.md#opentelemetry). Send to an OTel Collector that
routes traces/metrics to Langfuse, Tempo, or Datadog according to the backend's
supported signals. The SDK/exporter owns endpoint and authentication settings;
Linch does not require a vendor adapter.

---

## Related pages

- [Hooks](./hooks.md) — observe this stream via `on_event_emit` and react in-loop.
- [Evals and benchmarks](./evals.md) — run eval suites across deterministic targets.
- [Providers](./providers.md) — what `partial_assistant`/`usage` events carry per backend.
- [Agent](./agent.md) — `RunBudget` and the `budget` event.
