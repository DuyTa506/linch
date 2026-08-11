# Directed workflows (replayable fleet orchestration)

[← Usage guide](./README.md)

`agent.run_workflow(fn)` runs a plain async Python function that orchestrates subagents — your code owns the directed control flow, while subagents do the model-directed work. The journal makes an unchanged call prefix replayable and resumable. It is not called closed-loop: that term is reserved for a verifier whose feedback actually decides retry or stop.

Use a directed workflow when you already know the shape of the orchestration (fan out, verify, aggregate) and want the same `run_id` to replay an unchanged prefix. If instead you want the model itself to decide which subagents to spawn and when, use [coordinator mode](./deep-agent.md) — a workflow trades that flexibility for code-owned planning and resumability.

---

## `run_workflow(fn)`

You pass an async function that receives a single `wf` context. Everything the function `await`s on `wf` becomes a step in the run; the function's return value becomes the workflow result.

```python
from linch import Agent, RunBudget, SqliteRunStore

async def review(wf):
    await wf.phase("Find")
    findings = await wf.parallel([
        lambda: wf.agent("review the diff for bugs", label="bugs"),
        lambda: wf.agent("review the diff for performance", label="perf"),
    ])
    await wf.phase("Verify")
    verdicts = await wf.pipeline(
        findings,
        lambda f: wf.agent(f"adversarially verify: {f}"),
    )
    return verdicts

agent = Agent(model="gpt-5", run_store=SqliteRunStore("runs.db"))
result = await agent.run_workflow(
    review,
    budget=RunBudget(max_tokens=500_000),   # shared across every wf.agent child
    run_id="review-pr-42",                  # enables journaling + resume
    on_event=lambda e: print(e.type),       # WorkflowEvents + child SubagentEvents
)
```

The keyword arguments to `run_workflow` control budget, journaling, and observation:

- `budget` — a shared `RunBudget` that caps tokens/cost across the whole workflow and every `wf.agent` child it spawns. See [Run budgets](./agent.md) for the budget semantics.
- `run_id` — a stable identifier. Combined with an `Agent(run_store=...)`, it turns on journaling and resume (see below).
- `on_event` — a callback that receives both `WorkflowEvent`s (phase, agent/step start, end and replay) and the child `SubagentEvent`s emitted by each `wf.agent` call.
- `max_concurrency` — how many branches one `wf.parallel` fan-out may run at once (4 if unset). Each nesting level gets its own budget, so a `wf.parallel` inside a `wf.parallel` branch is capped independently rather than competing for — and deadlocking on — the slots its own caller is holding.
- `max_agent_concurrency` — how many `wf.agent` calls may hold a *live provider slot* at once (unlimited if unset). This is the provider-backpressure knob, deliberately separate from `max_concurrency`'s fan-out shape: it caps real spend regardless of how branches nest, and never gates a replayed call.
- `step_timeout_ms` — the default per-call wall-clock budget for `wf.agent` and `wf.step`. Each call can override it, and `0` opts a call out. An expired call raises `WorkflowTimeoutError`.
- `deadline_ms` — a wall-clock cap on the whole workflow. When it expires the function is cancelled and `WorkflowTimeoutError` is raised; the journaled prefix survives, so a resume picks up where it stopped.
- `resume` — a `{interrupt_key: value}` mapping that answers `wf.interrupt` calls left pending by an earlier suspend. See [Human in the loop](#human-in-the-loop).
- `signal` — an `AbortContext` that stops the workflow at its next journaled call and propagates into every subagent it spawns.
- `journal_snapshot_every` — checkpoint the journal every N records so a resume folds only the events after it instead of the whole log (off by default; it trades a periodic write for a one-shot load, which only pays off for long runs).

---

## The `wf` context

The `wf` argument is a `linch.workflow.WorkflowContext`. It exposes a small, fixed surface — you compose your orchestration out of these primitives rather than calling the loop directly:

- `await wf.agent(prompt, *, name=None, label=None, tools=None, run_options=None, output_schema=None, final_tool_name=None, timeout_ms=None, retry=None) -> str` — run a subagent (a named definition from the subagent registry, or the built-in general-purpose one) and return its final text. When the child produces `structured_output`, the returned string is compact JSON. A failed child raises `WorkflowError`.
- `await wf.step(name, fn, *, key=None, timeout_ms=None, retry=None) -> Any` — run *fn* (a zero-argument callable, sync or async) as a journaled step and return its value. This is how deterministic code, a tool call, or any other side-effecting work becomes a replayable node instead of something that re-runs on every resume. See [Journaling your own code](#journaling-your-own-code) below.
- `await wf.interrupt(key, payload=None) -> Any` — park the workflow until someone supplies an answer for *key*. See [Human in the loop](#human-in-the-loop).
- `await wf.parallel(thunks) -> list` — run thunks concurrently, capped by `max_concurrency` (default 4); results keep input order. A failing branch cancels its siblings.
- `await wf.settled(thunks) -> list[StepOutcome]` — like `parallel`, but a failing branch leaves its siblings running. See [Failure policy](#failure-policy).
- `await wf.pipeline(items, *stages) -> list` — run each item through all stages independently, with no barrier between stages.
- `await wf.phase(title)` — emit a progress phase marker.
- `wf.budget` — the shared `RunBudget` (see [Run budgets](./agent.md)).

A few practical notes:

- `wf.parallel` takes *thunks* (zero-argument lambdas), not coroutines, so the context can schedule them under its concurrency semaphore. Wrap each call as `lambda: wf.agent(...)`.
- When a `wf.parallel` branch raises, the remaining branches are cancelled and drained before the error propagates — a failed fan-out stops spending immediately. Cancelled branches are not journaled, so they re-run on resume.
- `wf.pipeline` runs each item through every stage independently — there is no barrier between stages, so a slow item never blocks a fast one. This differs from `parallel` + a second `parallel`, which would wait for the whole first batch.
- `name` selects a registered subagent definition; `label` is a human-readable tag that flows into the emitted events for tracing.

---

## Journal and resume

With `run_id` and a `run_store`, each `wf.agent` call's result is persisted as a `WorkflowEvent(kind="agent_end")` in the run's event log. Re-invoking `run_workflow` with the same `run_id` replays the unchanged call prefix from that journal — `kind="agent_replayed"` events fire instead of provider calls. Calls are keyed by a content hash of `(subagent_type, prompt, tools, run_options)` plus an occurrence counter, so identical parallel calls replay safely. Editing a prompt, structured-output option, or tool filter invalidates that call rather than reusing a result produced under a different tool policy.

The `WorkflowEvent` kinds you will see on the `on_event` stream are:

| Kind | When it fires |
|------|---------------|
| `phase` | You call `await wf.phase(title)` |
| `agent_start` | A `wf.agent` call begins a live subagent run |
| `agent_end` | A `wf.agent` call completes; the result is journaled |
| `agent_replayed` | A resumed run found a matching journaled result and skipped the provider call |
| `step_start` | A `wf.step` call begins executing its function |
| `step_end` | A `wf.step` call completes; the JSON-encoded value is journaled in `result_text` |
| `step_replayed` | A resumed run found a matching journaled value and skipped the function |
| `interrupt_requested` | A `wf.interrupt` had no answer; the run is about to suspend. `structured_output` carries `{"payload": ...}` |
| `interrupt_resolved` | An answer from `resume=` was accepted and journaled |
| `interrupt_replayed` | A resumed run found a journaled answer and did not suspend again |

Because the journal is keyed by content hash, resume is precise: editing one prompt in the middle of a workflow invalidates only that call and everything after it, while earlier calls still replay from the journal.

The call-key formula itself is versioned and pinned per run at creation (stored in the run's meta), so upgrading the linch SDK never changes how an already-in-flight run's existing calls are keyed — a resumed run keeps replaying its unchanged prefix under whichever formula it started with, rather than silently re-executing it under a newer one.

---

## Journaling your own code

Only journaled calls replay. `wf.agent` is journaled and so is `wf.step`; everything else in the workflow function re-executes on every resume:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await post_to_github(findings)              # runs again on every resume
    return await wf.agent("summarize")
```

Wrap that side effect in a step and it happens exactly once across the whole run, however many times you resume:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await wf.step("publish", lambda: post_to_github(findings))
    return await wf.agent("summarize")
```

The step's `name` is its durable identity — renaming it invalidates that step and everything after it, exactly like editing a prompt. Pass `key=` when the same step runs over different inputs, so each input journals separately instead of being told apart only by call order:

```python
for url in urls:
    await wf.step("fetch", lambda u=url: http.get(u), key=url)
```

A step's value is journaled as JSON, so it must be JSON-serializable. `wf.step` raises `ConfigError` otherwise — whether or not a run store is configured, so a value that could never replay fails in development rather than the first time you pass `run_id`.

---

## Failure policy

By default a node gets one attempt, and a failure propagates. Two knobs change that.

`retry=` re-runs a failed `wf.agent` or `wf.step` with exponential backoff. Only the winning attempt is journaled, so a retried node still replays exactly once — but *fn* has to be safe to run more than once:

```python
from linch import RetryOptions

await wf.agent("review the diff", retry=RetryOptions(max_attempts=3))
await wf.step("fetch", lambda: http.get(url), key=url, retry=RetryOptions(max_attempts=3))
```

Failed attempts emit no journal event at all, so a node that exhausts its retries re-runs from scratch on the next resume rather than replaying a failure. A `ConfigError` — an unknown subagent name, a value that cannot be JSON-encoded — is never retried, because it fails identically every time.

`wf.settled` is the opt-out from sibling cancellation. Where `wf.parallel` cancels the remaining branches as soon as one raises, `settled` lets them all finish and hands you one `StepOutcome` per branch, in input order:

```python
outcomes = await wf.settled([
    lambda: wf.agent("review for bugs"),
    lambda: wf.agent("review for perf"),
])
findings = [o.value for o in outcomes if o.ok]
for failed in (o for o in outcomes if not o.ok):
    print(failed.error)
```

`StepOutcome` has `.ok`, `.value`, and `.error`. Cancellation and suspension are control flow rather than branch outcomes: they propagate instead of landing in a slot.

---

## Human in the loop

`wf.interrupt(key, payload)` parks a workflow at a decision point. The first time through there is no answer, so the run emits an `interrupt_requested` event carrying the payload, is marked `"suspended"` in the run store — **not** failed — and `WorkflowSuspended` is raised out of `run_workflow`:

```python
from linch import WorkflowSuspended

async def flow(wf):
    plan = await wf.agent("draft a migration plan")
    approved = await wf.interrupt("approve-plan", {"plan": plan})
    if not approved:
        return "cancelled"
    return await wf.step("apply", lambda: apply_migration(plan))

try:
    await agent.run_workflow(flow, run_id="migrate-42")
except WorkflowSuspended as suspended:
    show_to_reviewer(suspended.key, suspended.payload)
```

Supply the answer by re-invoking the same `run_id` with a `resume` mapping. The journaled prefix replays, the interrupt returns the value, and the run continues:

```python
await agent.run_workflow(flow, run_id="migrate-42", resume={"approve-plan": True})
```

The answer is journaled like any other node, so a *later* resume needs no `resume` argument — it replays as `interrupt_replayed`. The value must be JSON-serializable, as must the payload.

Three things to know:

- `WorkflowSuspended` extends `BaseException`, not `Exception`, so an `except Exception:` inside your workflow function cannot swallow a suspend by accident.
- A `resume` entry is consumed once. A second `wf.interrupt` under the same key suspends again rather than silently reusing the earlier decision — which also means interrupts inside a loop need distinct keys.
- Interrupting inside a `wf.parallel` branch cancels the sibling branches, so a fan-out cannot collect several answers at once. Ask for the decisions sequentially, or use `wf.settled`.

---

## Replay rule

The workflow function must keep its control flow deterministic — no `random`, wall-clock, or environment-dependent branching — for resume to replay the unchanged prefix correctly.

Branching on a journaled result is fine: `if "LGTM" in await wf.agent(...)` replays identically, because the result itself replays. What breaks resume is variability the journal never saw — a random sample, the current time, an environment variable, an HTTP response fetched by bare code. Put that variability inside a `wf.step` (or inside a subagent prompt) where the content hash can track it, and the resumed run follows the same path it took the first time.

---

## Related pages

- [Deep agent preset](./deep-agent.md) — the LLM-driven coordinator alternative
- [Agent configuration](./agent.md) — `RunBudget` and durable stores
- [Events](./events.md) — consuming the `on_event` stream
- [Architecture](../architecture.md) — how the fleet loop fits the overall design
