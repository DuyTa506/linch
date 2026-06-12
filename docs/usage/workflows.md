# Workflows (deterministic fleet loops)

[← Usage guide](./README.md)

`agent.run_workflow(fn)` runs a plain async Python function that orchestrates subagents deterministically — your script owns the control flow, subagents do the work. This is the "closed loop" counterpart to the LLM-driven coordinator mode: cheap, repeatable, and resumable.

Use a workflow when you already know the shape of the orchestration (fan out, verify, aggregate) and want guarantees that the same `run_id` replays identically. If instead you want the model itself to decide which subagents to spawn and when, use [coordinator mode](./deep-agent.md) — a workflow trades that flexibility for determinism and resumability.

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
- `on_event` — a callback that receives both `WorkflowEvent`s (phase, agent_start/end, replay) and the child `SubagentEvent`s emitted by each `wf.agent` call.
- `max_concurrency` — the default cap for `wf.parallel` (4 if unset).

---

## The `wf` context

The `wf` argument is a `linch.workflow.WorkflowContext`. It exposes a small, fixed surface — you compose your orchestration out of these primitives rather than calling the loop directly:

- `await wf.agent(prompt, *, name=None, label=None, tools=None) -> str` — run a subagent (a named definition from the subagent registry, or the built-in general-purpose one) and return its final text. A failed child raises `WorkflowError`.
- `await wf.parallel(thunks) -> list` — run thunks concurrently, capped by `max_concurrency` (default 4); results keep input order.
- `await wf.pipeline(items, *stages) -> list` — run each item through all stages independently, with no barrier between stages.
- `await wf.phase(title)` — emit a progress phase marker.
- `wf.budget` — the shared `RunBudget` (see [Run budgets](./agent.md)).

A few practical notes:

- `wf.parallel` takes *thunks* (zero-argument lambdas), not coroutines, so the context can schedule them under its concurrency semaphore. Wrap each call as `lambda: wf.agent(...)`.
- `wf.pipeline` runs each item through every stage independently — there is no barrier between stages, so a slow item never blocks a fast one. This differs from `parallel` + a second `parallel`, which would wait for the whole first batch.
- `name` selects a registered subagent definition; `label` is a human-readable tag that flows into the emitted events for tracing.

---

## Journal and resume

With `run_id` and a `run_store`, each `wf.agent` call's result is persisted as a `WorkflowEvent(kind="agent_end")` in the run's event log. Re-invoking `run_workflow` with the same `run_id` replays the unchanged call prefix from that journal — `kind="agent_replayed"` events fire instead of provider calls. Calls are keyed by a content hash of `(subagent_type, prompt)` plus an occurrence counter, so identical parallel calls replay safely and an edited prompt invalidates only that call.

The `WorkflowEvent` kinds you will see on the `on_event` stream are:

| Kind | When it fires |
|------|---------------|
| `phase` | You call `await wf.phase(title)` |
| `agent_start` | A `wf.agent` call begins a live subagent run |
| `agent_end` | A `wf.agent` call completes; the result is journaled |
| `agent_replayed` | A resumed run found a matching journaled result and skipped the provider call |

Because the journal is keyed by content hash, resume is precise: editing one prompt in the middle of a workflow invalidates only that call and everything after it, while earlier calls still replay from the journal.

---

## Determinism rule

The workflow function must be deterministic — no `random`, wall-clock, or environment-dependent branching — for resume to replay the unchanged prefix correctly.

If your control flow branches on something non-deterministic (a random sample, the current time, an external API result not captured in a subagent prompt), a resumed run can diverge from the original and the journaled prefix will no longer line up with the calls being made. Keep all variability inside the subagent prompts, where the content hash can track it.

---

## Related pages

- [Deep agent preset](./deep-agent.md) — the LLM-driven coordinator alternative
- [Agent configuration](./agent.md) — `RunBudget` and durable stores
- [Events](./events.md) — consuming the `on_event` stream
- [Architecture](../architecture.md) — how the fleet loop fits the overall design
