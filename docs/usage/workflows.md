# Directed workflows (replayable fleet orchestration)

Use `run_workflow` when your code should direct and replay a known orchestration sequence.

[← Usage guide](./README.md)

`run_workflow(agent, fn)` runs a plain async Python function that orchestrates subagents — your code owns the directed control flow, while subagents do the model-directed work. With a run store, the journal makes matching calls replayable and a durable run resumable.

Use a directed workflow when you already know the shape of the orchestration (fan out, verify, aggregate) and want the same `run_id` to replay matching calls. If instead you want the model itself to decide which subagents to spawn and when, use [coordinator mode](./deep-agent.md) — a workflow trades that flexibility for code-owned planning and resumability.

---

<a id="run_workflowfn"></a>

## `run_workflow(agent, fn)`

You pass an async function that receives a single `wf` context. The
journaled primitives (`wf.agent`, `wf.step`, and `wf.interrupt`) become
replayable records; the fan-out and pipeline helpers orchestrate work, while
`wf.phase` emits progress. The function's return value becomes the workflow
result.

```python
import os
from linch import Agent, RunBudget, PostgresRunStore
from linch.ext.workflow import run_workflow

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

agent = Agent(model="gpt-5", run_store=PostgresRunStore(os.environ["LINCH_POSTGRES_DSN"]))
result = await run_workflow(
    agent,
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
- `max_concurrency` — how many branches one `wf.parallel` fan-out may run at once (4 if unset). Each nesting level gets its own semaphore, so a nested `wf.parallel` is capped independently rather than competing for — and deadlocking on — the slots its own caller is holding.
- `max_agent_concurrency` — how many `wf.agent` calls may hold a *live provider slot* at once (unlimited when `0`, the default). This is the provider-backpressure knob, deliberately separate from `max_concurrency`'s fan-out shape: it caps real spend regardless of how branches nest, and never gates a replayed call.
- `step_timeout_ms` — the default per-attempt wall-clock budget for `wf.agent` and `wf.step`. Each call can override it, and `0` opts a call out. An expired call raises `WorkflowTimeoutError`.
- `deadline_ms` — a wall-clock cap on the whole workflow. When it expires the function is cancelled and `WorkflowTimeoutError` is raised; for a durable run, completed journal entries survive, so a resume can replay matching calls and continue.
- `resume` — a `{interrupt_key: value}` mapping that answers `wf.interrupt` calls left pending by an earlier suspend. See [Human in the loop](#human-in-the-loop).
- `signal` — an `AbortContext` that stops the workflow at its next journaled call and propagates into every subagent it spawns.
- `journal_snapshot_every` — checkpoint the journal every N records so a resume folds only the events after it instead of the whole log (off by default; it trades a periodic write for a one-shot load, which only pays off for long runs).
- `lease` — an optional `RunLease` that fences durable workflow writes; the engine renews and releases it while the workflow is active.

---

<a id="the-wf-context"></a>

## `WorkflowContext`

The `wf` argument is a `linch.ext.workflow.WorkflowContext`. It exposes a small, fixed surface — you compose your orchestration out of these primitives rather than calling the loop directly:

- `await wf.agent(prompt, *, name=None, label=None, tools=None, run_options=None, key=None, output_schema=None, final_tool_name=None, fork=False, isolation=None, isolation_keep=False, timeout_ms=None, retry=None) -> str` — run a subagent (a named definition from the subagent registry, or the built-in general-purpose one) and return its final text. When the child produces `structured_output`, the returned string is compact JSON. A failed child raises `WorkflowError`; an aborted child raises `AbortError`. `fork=True` continues from the workflow host's conversation prefix, while `isolation=` gives the child a separate working directory for conflicting parallel edits.
- `await wf.step(name, fn, *, key=None, timeout_ms=None, retry=None) -> Any` — run *fn* (a zero-argument callable, sync or async) as a journaled step and return its value. This is how deterministic code, a tool call, or any other side-effecting work becomes a replayable node instead of something that re-runs on every resume. See [Journaling your own code](#journaling-your-own-code) below.
- `await wf.interrupt(key, payload=None) -> Any` — park the workflow until someone supplies an answer for *key*. See [Human in the loop](#human-in-the-loop).
- `await wf.parallel(thunks) -> list` — run thunks concurrently, capped by `max_concurrency` (default 4); results keep input order. A failing branch cancels its siblings.
- `await wf.settled(thunks) -> list[StepOutcome]` — like `parallel`, but a failing branch leaves its siblings running. See [Failure policy](#failure-policy).
- `await wf.pipeline(items, *stages) -> list` — run each item through all stages independently, with no barrier between stages.
- `await wf.phase(title)` — emit a progress phase marker.
- `wf.budget` — the shared `RunBudget` (see [Run budgets](./agent.md)).

A few practical notes:

- `wf.parallel` takes *thunks* (zero-argument callables returning awaitables), not already-created coroutines, so the context can schedule them under its concurrency semaphore. Wrap each call as `lambda: wf.agent(...)`.
- When a `wf.parallel` branch raises, the remaining branches are cancelled and drained before the error propagates — a failed fan-out stops spending immediately. Cancelled branches are not journaled, so they re-run on resume.
- `wf.pipeline` runs each item through every stage independently — there is no barrier between stages, so a slow item never blocks a fast one. This differs from `parallel` + a second `parallel`, which would wait for the whole first batch.
- `name` selects a registered subagent definition; `label` is a human-readable tag that flows into the emitted events for tracing.

---

<a id="journal-and-resume"></a>

## `WorkflowJournal` — journal and resume

With `run_id` and a `run_store`, each `wf.agent` and `wf.step` result is
persisted as a `WorkflowEvent` (`agent_end` or `step_end`) in the run's event
log. Re-invoking `run_workflow` with the same `run_id` replays matching calls
from that journal — `agent_replayed` or `step_replayed` events fire instead of
a provider call or step function. Agent calls are keyed by a content hash of
`(subagent_type, prompt, tools, run_options)` plus an occurrence counter, so
identical parallel calls replay safely. With automatic identities, editing a
prompt, structured-output option, or tool filter makes that call miss its old
key rather than reusing a result produced under a different tool policy; later
calls whose own keys still match may still replay. With `key=`, the caller owns
the identity and must change it when those dependencies change.

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

Because the journal is keyed by content hash, resume is precise: editing one
prompt in the middle of a workflow invalidates that call, while any earlier or
later call whose own key still matches can replay from the journal.

Durable workflow runs require `schema_version=3` in their metadata and use the
current tools-aware call-key formula. Missing, old, or unknown metadata versions
are rejected; start a new workflow run. Replay arguments use canonical JSON
encoding, with sets sorted; unsupported values raise `ConfigError` before
execution when automatic identity is used. An explicit `wf.agent(key=...)`
bypasses serialization of `run_options` and dependencies; the caller-owned key
and any tool filter still need JSON-fingerprintable values. Tool filters remain
part of the hash, and the caller-owned key must include policy and dependency
revisions that should invalidate replay.

---

## Journaling your own code

Only journaled calls replay. `wf.agent` is journaled and so is `wf.step`;
everything else in the workflow function re-executes on every resume:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await post_to_github(findings)              # runs again on every resume
    return await wf.agent("summarize")
```

Wrap that side effect in a step so resume replays its durably journaled result. The side effect still needs idempotency for a process crash before that journal write:

```python
async def flow(wf):
    findings = await wf.agent("review the diff")
    await wf.step("publish", lambda: post_to_github(findings))
    return await wf.agent("summarize")
```

The step's `name` is its durable identity — renaming it invalidates that step's old journal entry, exactly like editing a prompt. Later calls whose own keys still match may replay. Pass `key=` when the same step runs over different inputs, so each input journals separately instead of being told apart only by call order:

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

Failed attempts do not emit a journaled result/end record, so a node that
exhausts its retries re-runs from scratch on the next resume rather than
replaying a failure. The surrounding `*_start` event may still be observed. A
`ConfigError` — an unknown subagent name, a value that cannot be JSON-encoded —
is never retried, because it fails identically every time.

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

`wf.interrupt(key, payload)` parks a workflow at a decision point. The first
time through there is no answer, so a durable run emits an
`interrupt_requested` event carrying the payload, is marked `"suspended"` in
the run store — **not** failed — and `WorkflowSuspended` is raised out of
`run_workflow`:

```python
from linch.ext.workflow import WorkflowSuspended

async def flow(wf):
    plan = await wf.agent("draft a migration plan")
    approved = await wf.interrupt("approve-plan", {"plan": plan})
    if not approved:
        return "cancelled"
    return await wf.step("apply", lambda: apply_migration(plan))

try:
    await run_workflow(agent, flow, run_id="migrate-42")
except WorkflowSuspended as suspended:
    show_to_reviewer(suspended.key, suspended.payload)
```

Supply the answer by re-invoking the same `run_id` with a `resume` mapping. Matching journaled calls replay, the interrupt returns the value, and the run continues:

```python
await run_workflow(agent, flow, run_id="migrate-42", resume={"approve-plan": True})
```

The answer is journaled like any other node, so a *later* resume needs no `resume` argument — it replays as `interrupt_replayed`. The value must be JSON-serializable, as must the payload.

Three things to know:

- `WorkflowSuspended` extends `BaseException`, not `Exception`, so an `except Exception:` inside your workflow function cannot swallow a suspend by accident.
- A `resume` entry is consumed once. A second `wf.interrupt` under the same key suspends again rather than silently reusing the earlier decision — which also means interrupts inside a loop need distinct keys.
- Interrupting inside a `wf.parallel` branch cancels the sibling branches, so a fan-out cannot collect several answers at once. Ask for the decisions sequentially. If sibling work should finish before suspension, use `wf.settled`; it still propagates `WorkflowSuspended` rather than collecting several answers.

---

## Running across processes

`linch.worker` does not dispatch workflows: its jobs run a prompt through a
session, not a workflow function. Your host decides which process calls
`run_workflow`, for example from its own job queue or service. Durable workflow
state is still safe to share: use a Postgres `run_store` and a stable `run_id`, and
any process with the same workflow function can continue the run.

- **Crash recovery is the host's job.** Nothing re-invokes an interrupted workflow
  for you. Find unfinished runs and call `run_workflow` again with the same
  `run_id`.
- **Expect a wait after a crash.** Without `lease=`, `run_workflow` claims the run
  for five minutes and renews while it is alive. A process that dies leaves that
  lease in place, so re-invoking early raises `RunLeaseConflictError`. Retry with
  backoff. The same error stops two processes running one workflow at once.
- **Answers can come from anywhere.** A suspended run holds no process. Any host
  process can answer it by calling `run_workflow` with `resume=`.
- **Inside a workflow, `max_concurrency` applies per process.** Subagents run in
  the process that called `run_workflow`. To use a fleet, split the work into one
  workflow per item and let your queue spread them.

---

## Replay rule

The workflow function must keep its control flow deterministic — no `random`, wall-clock, or environment-dependent branching — for resume to replay the unchanged journaled calls correctly.

Branching on a journaled result is fine: `if "LGTM" in await wf.agent(...)`
replays identically, because the result itself replays. What breaks resume is
variability the journal never saw — a random sample, the current time, an
environment variable, or an HTTP response fetched by bare code. Put that
variability inside a `wf.step` so its returned value is journaled (and use
`key=` when the step's input identity needs to be explicit). The resumed run
then follows the path it took the first time.

---

## Related pages

- [Deep agent preset](./deep-agent.md) — the LLM-driven coordinator alternative
- [Agent configuration](./agent.md) — `RunBudget` and durable stores
- [Events](./events.md) — consuming the `on_event` stream
- [Architecture](../architecture.md) — how the fleet loop fits the overall design
