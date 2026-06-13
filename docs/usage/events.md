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
        case "user":      # user message appended
        case "assistant": # full assistant turn (final)
        case "partial_assistant":  # streaming text/thinking delta
        case "tool_call_start":   # tool about to run
        case "tool_call_end":     # tool finished, has .result
        case "permission_request": # user approval needed (mode="default")
        case "usage":     # token counts for this turn
        case "budget":    # RunBudget warning (90%) or exhaustion
        case "result":    # run finished — .subtype in ("success","error","aborted")
        case "error":     # provider/tool error details
        case "compaction": # context was summarised (or micro-compacted)
        case "workflow":  # workflow engine progress / journal records
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
- **`tool_call_end` carries the result** on `.result`, including the rich
  `ToolResult` fields (summary, metadata, citations) even when the model only
  saw a truncated/offloaded preview.
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

`event.subtype` is one of `"success"`, `"error"`, or `"aborted"` — branch on it
to decide whether to surface the answer, an error, or a cancellation.

The same event stream is mirrored to hooks at the `on_event_emit` chokepoint, so
anything you can do in this loop you can also do in a reusable hook — see
[Hooks](./hooks.md).

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

`usage` events report the cost of the turn (`cost_usd`) and the running total so
far (`cumulative_cost_usd`); the terminal `result` event reports the run total
(`total_cost_usd`).

**Unknown model IDs report `None`** for every cost field rather than charging a
synthetic price — this is deliberate so a private or self-hosted model never
shows a misleading number. To attribute cost for such models, pass a custom
table:

```python
# linch.pricing.cost_usd(usage, model, table=...) with a custom table
# for private or self-hosted models.
```

This matters for budgets too: with an unpriced model, only the token limit of a
`RunBudget` binds (the cost limit can never trip). See
[Agent](./agent.md) for budget configuration.

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
  `<task-notification>` on a later turn — that path intentionally does *not* block
  the foreground stream. See [Tools](./tools.md) and [deep-agent](./deep-agent.md).

---

## Run reports

Use run reports when you need a compact debugging export for a finished or
in-progress run. Reports are built from typed events and, when available,
`RunStore` checkpoints. They are a pure **read model** — building one never
mutates session state.

Load a report from a durable run store by `run_id`:

```python
from linch import load_run_report

report = await load_run_report(agent.run_store, run_id)
print(report.to_markdown())

payload = report.to_dict()
print(payload["context_builds"])
print(payload["permission_requests"])
print(payload["tool_calls"])
print(payload["loop_guards"])
print(payload["long_run"])
```

For transient runs where you already collected events, build a report directly
from the list you accumulated — no store required:

```python
from linch import build_run_report

events = []
async for event in session.run("Summarize the policy change."):
    events.append(event)

report = build_run_report(events)
```

Use `to_markdown()` for a human-readable dump (logs, PR comments) and
`to_dict()` when you want to inspect or serialize specific facets.

`payload["long_run"]` summarizes signals that matter in long-horizon sessions:
context build counts, trimmed context builds, max used context tokens, selected
tool counts, memory searches/upserts, recalled memory ids, tier counts, failed
tool calls, recovery hints, completion status, total cost, and checkpoint phase.

---

## Long-run eval scorers

The run report tells you what *one* run did; the **eval harness** grades many
runs deterministically offline. Its scorers cover context and memory behavior in
addition to the usual text, schema, tool, and cost checks — each returns
`True`/`False`/`None`:

```python
from linch.evals import (
    context_metadata_contains,
    context_not_trimmed,
    context_selected_tool,
    cost_under,
    memory_recalled,
    recovery_succeeded,
    run_completed,
    run_eval,
)

result = await run_eval(
    agent,
    cases,
    scorers=[
        context_selected_tool("SearchMemory"),
        context_metadata_contains("memory_namespace", "user:42"),
        context_not_trimmed(),
        memory_recalled("pref-1"),
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
behavior without spending a live model call.

---

## Related pages

- [Hooks](./hooks.md) — observe this stream via `on_event_emit` and react in-loop.
- [Providers](./providers.md) — what `partial_assistant`/`usage` events carry per backend.
- [Agent](./agent.md) — `RunBudget` and the `budget` event.
