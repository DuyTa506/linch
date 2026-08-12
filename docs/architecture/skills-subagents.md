# Skills and Subagents

> Part of the [Linch architecture guide](./README.md).

**Skills** are loaded from `.linch/skills/*/SKILL.md`; built-in skills
such as `verify` are also registered unless a disk skill uses the same name.
Each file has YAML frontmatter (`name`, `description`, `allowed_tools`,
`model_override`) and a markdown body. When a skill is invoked, the body is
injected as a `<system-reminder>` per-turn via `_re_inject_skill_context`.
Gated by `FeatureFlags(skills=True)`. Linch 2.0's bare `Agent` sets this flag
to `False`, so an SDK embedder does not discover project instructions merely
because its process is running in a checkout.

**Subagents** are defined in `.linch/agents/*.md`; built-in named agents
such as `verification` are also registered unless a disk agent uses the same
name. `subagents/runner.py` creates a child agent with its own tool overlay and
system prompt. The child's system blocks are computed from its own tool names —
not copied from the parent. Gated by `FeatureFlags(subagents=True)`.

The bare SDK agent also leaves `subagents`, `mcp`, and `filesystem` disabled.
Enable each trusted project resource explicitly, or use the opt-in
`create_deep_agent()` preset, which enables the capabilities it assembles.

**MCP** — `connect_mcp_servers(configs)` wraps each MCP tool as a duck-typed Linch tool. Names are normalized via `mcp/naming.py`. The connection closes on `agent.close()`. Gated by `FeatureFlags(mcp=True)`.

### Deep agent preset (`deep_agent/`)

`create_deep_agent(model, profile, durable, coordinator, cwd, ...)` is an
explicit factory that assembles a full deep-agent configuration in one call:
task tools, a specialist subagent roster (researcher, planner, implementer),
durable stores, a persistent `/memories` filesystem, and a deepened system
prompt. The default `balanced` profile bounds a run to 64 turns and 1,000,000
tokens while retaining workers; `coordinator` has the same bounds and strips
heavy tools from the parent. `unbounded` is an explicit escape hatch for a
host that owns equivalent lifetime and cost controls.

- **`coordinator=True`** — the parent agent strips heavy tools (`Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Read`); `COORDINATOR_SYSTEM_PROMPT` is injected via `SystemPromptConfig`; `TaskStopTool` is registered on the coordinator. Worker subagents receive full tool access via `build_child_tools`.
- **`durable=True`** — wires `SqliteSessionStore` + `SqliteRunStore` + `CompositeFileBackend` with a `/memories` route to `SqliteFileBackend` so session history, checkpoints, and memory data persist across restarts. An active detached worker task is process-local and is not reconstructed after a crash.

### Background workers and fork/continue

```mermaid
sequenceDiagram
    participant P as Parent loop
    participant ST as SubagentTool
    participant W as Worker (child session)
    participant N as pending_notifications (in-memory)
    participant RS as RunStore audit log

    P->>ST: call (run_in_background=true)
    ST->>W: asyncio.create_task(_bg_run)
    ST-->>P: ack — turn continues, not blocked
    W->>W: run to completion (retain=true → stays in agent._sessions)
    W->>N: append <task-notification> (in-memory)
    W->>RS: append origin-attributed audit event (when configured)
    Note over P,N: top of the next turn
    P->>N: _drain_pending_notifications
    N-->>P: UserEvent per notification (before context build)
    alt continue
        P->>W: SubagentContinueTool → continue_subagent (full prior provider_view)
    else stop
        P->>W: TaskStopTool → cancel task + abort; handle stays continuable
    end
```

- `SubagentTool` always passes `retain=True` so the child session stays live in `agent._sessions` after the run ends.
- `session.workers: dict[str, WorkerHandle]` indexes every spawned worker by `worker_id`.
- **`run_in_background=True`** on `SubagentTool`: spawns `asyncio.create_task(_bg_run())` and returns an acknowledgement immediately. On completion, the task appends a `<task-notification>` XML `Message` to `session.pending_notifications`. That notification is in-memory only. When a `RunStore` is configured, Linch also records an origin-attributed `BackgroundWorkerEvent` audit record, but it does not turn the detached result into a restart-recoverable queue.
- The loop drains `session.pending_notifications` at the top of each turn (`_drain_pending_notifications`), yielding each notification as a `UserEvent` before `ContextInjectionHook.build_context()` runs, so the model sees task-completion content before the next provider call.
- `SubagentContinueTool` resolves a worker by id or display name via `resolve_worker`, then calls `continue_subagent()`, which re-drives the live child session using the full prior `provider_view`.
- `TaskStopTool` cancels the background `asyncio.Task` and signals abort on the child session; the `WorkerHandle` remains in `session.workers` so the worker can be continued later.
- `session.abort()` and `agent.close()` both cancel all running background worker tasks. `agent.close()` additionally clears `agent._sessions`.
- A process restart cannot resume an active detached task or recreate its
  in-memory completion notification. Hosts that need durable worker delivery
  must persist the work and result in an application-owned queue or workflow;
  the audit event is evidence of state, not the result payload delivery path.

### Run budgets (`budget.py`)

`RunBudget` is a plain mutable accumulator capping tokens and/or USD for a run
**and its whole subagent tree**. Resolution order in `run_loop`:
`RunOptions.budget` → `session.inherited_budget` (set on child sessions by
`run_subagent` from the parent's `active_budget`) → `Agent(budget=...)`. The
loop charges the budget after every provider turn (next to the `UsageEvent`)
and checks `exceeded` before each turn; exhaustion emits
`BudgetEvent(kind="exceeded")` → `ErrorEvent(BudgetExceededError)` →
`ResultEvent(subtype="error")` and stops gracefully — history intact, session
reusable. A `BudgetEvent(kind="warning")` fires once per budget object at
`warn_ratio` (default 0.9). Because parent and children charge the *same
object*, child spending is visible to the parent's next pre-call check.

### Workflow engine (`workflow/`)

`agent.run_workflow(fn)` drives a deterministic "closed fleet loop": *fn* is a
plain async function receiving a `WorkflowContext` (`wf`) and orchestrating
subagents via `wf.agent` / `wf.step` / `wf.interrupt` / `wf.parallel` /
`wf.settled` / `wf.pipeline` / `wf.phase`, with `wf.budget` exposing the shared
`RunBudget`.

- A host session parents every `wf.agent` run (each is a normal
  `run_subagent` child); child `SubagentEvent`s and `WorkflowEvent`s reach the
  host via the `on_event` callback.
- **Two journaled step kinds, one code path.** `wf.agent` runs a subagent;
  `wf.step` runs an arbitrary sync/async callable so deterministic code and
  side effects are replayable too. Both go through `_journaled_call`, which
  owns occurrence assignment, journal lookup, the `*_start` / `*_end` /
  `*_replayed` events, and recording — so the two cannot drift.
- **Journal = the run event log.** With `Agent(run_store=...)` and a
  `run_id`, each result persists as `WorkflowEvent(kind="agent_end")` or
  `kind="step_end"`. Re-invoking with the same `run_id` folds the stored events
  back into a `WorkflowJournal` (see `JOURNALED_KINDS`) and replays the
  unchanged call prefix with no provider call and no re-execution. Agent calls
  are keyed by `sha256(subagent_type, prompt, call_options)`, steps by
  `sha256("step", name, key)` — disjoint domains, so a step can never collide
  with a subagent — each plus a per-key occurrence counter, so parallel fan-out
  and loops replay safely and an edit invalidates only the call it touched.
- A step's value crosses the journal as JSON in `result_text`; the record's
  `record_kind` discriminates it from an agent's final text.
- The workflow function must be deterministic (no random/time-based
  branching) for resume replay to be correct. Branching on a *journaled* result
  is deterministic by construction; unjournaled variability is not.
- `wf.parallel` cancels and drains its remaining branches when one raises, and
  gives each nesting level its own semaphore so a nested fan-out cannot
  deadlock on slots its own caller holds. `wf.settled` is the opt-out: it lets
  siblings finish and returns one `StepOutcome` per branch. Both re-raise
  `CancelledError` / `WorkflowSuspended` rather than reporting them as
  outcomes — those are control flow, not results.
- **Failure policy is per call, applied outside the journal lookup.**
  `_journaled_call` assigns the occurrence and consults the journal *once*,
  then hands the work to `_call_with_policy`, which applies `timeout_ms`
  (per attempt) and `retry` (reusing `providers/retry.with_retry` with a
  `retry_on` predicate). A retry that burned a fresh occurrence would record
  under the wrong slot and silently break replay. With neither policy set,
  neither a `wait_for` nor a `with_retry` frame is constructed.
- **`wf.interrupt` is the suspend point.** No answer means an
  `interrupt_requested` event and a `WorkflowSuspended` — a `BaseException`, so
  a user's `except Exception:` cannot swallow it. `run_workflow` catches it
  *before* its `except BaseException` arm and writes a
  `save_checkpoint(status="suspended")` instead of `mark_failed`. A `resume=`
  mapping supplies the answer, which is journaled like any other node and
  consumed once per key.
- **Two independent concurrency knobs.** `max_concurrency` shapes one
  `wf.parallel` fan-out; `max_agent_concurrency` is a lazily created second
  semaphore held only around the live `run_subagent` call, so it caps real
  provider pressure without gating the replay path. It cannot deadlock because
  `wf.agent` is a leaf.
- **The journal snapshot is an accelerator, never a source of truth.** With
  `journal_snapshot_every=N`, every Nth record checkpoints the journal into
  `RunCheckpoint.extension_state["linch.workflow"]` with the event-log
  watermark it covers; a resume seeds from it and folds only later events. A
  record is always journaled *before* its `*_end` event is appended, so
  anything the snapshot missed is guaranteed to be in that tail. Missing,
  oversized (`_SNAPSHOT_MAX_BYTES`) or malformed, it falls back to folding the
  whole log.

## Design rationale

- **A child computes its own system blocks from its own tools — never copies the
  parent's.** A subagent's prompt must describe the toolset it actually has;
  inheriting the parent's prompt would mis-describe its capabilities (and a
  tool-stripped coordinator would leak instructions for tools the child shouldn't
  use).
- **Background workers don't block the turn.** Spawning via `asyncio.create_task` and
  returning an ack immediately keeps the parent responsive; results re-enter at a
  *fixed chokepoint* (drained at the top of the next turn) rather than racing into
  mid-turn state — deterministic ordering instead of a callback free-for-all.
- **`retain=True` makes workers continuable.** Keeping the child session alive turns a
  one-shot subagent into a long-lived teammate (`continue_subagent` re-drives it with
  its full prior `provider_view`), without forcing every caller to manage session
  lifecycles.
- **The budget is one shared mutable object across the whole tree.** Children charge
  the *same* `RunBudget` the parent holds, so a fan-out can't escape the cap and child
  spend is visible to the parent's next pre-call check — a per-child copy would let the
  tree overspend silently.
- **Coordinator mode strips heavy tools on purpose.** Removing `Edit`/`Write`/`Bash`/…
  from the parent forces delegation: the coordinator orchestrates, workers execute.
  That separation is a safety rail, not a limitation.
- **Workflows must be deterministic so resume can replay by content.** The journal keys
  each call by `sha256(subagent_type, prompt, run_options)`; nondeterministic branching
  would replay the wrong prefix, so determinism is the price of cheap, correct resume.

---

Back to the [architecture index](./README.md).
