# Skills and Subagents

> Part of the [Linch architecture guide](./README.md).

**Skills** are loaded from `.linch/skills/*/SKILL.md`; built-in skills
such as `verify` are also registered unless a disk skill uses the same name.
Each file has YAML frontmatter (`name`, `description`, `allowed_tools`,
`model_override`) and a markdown body. When a skill is invoked, the body is
injected as a `<system-reminder>` per-turn via `_re_inject_skill_context`.
Gated by `FeatureFlags(skills=True)`.

**Subagents** are defined in `.linch/agents/*.md`; built-in named agents
such as `verification` are also registered unless a disk agent uses the same
name. `subagents/runner.py` creates a child agent with its own tool overlay and
system prompt. The child's system blocks are computed from its own tool names —
not copied from the parent. Gated by `FeatureFlags(subagents=True)`.

**MCP** — `connect_mcp_servers(configs)` wraps each MCP tool as a duck-typed Linch tool. Names are normalized via `mcp/naming.py`. The connection closes on `agent.close()`. Gated by `FeatureFlags(mcp=True)`.

### Deep agent preset (`deep_agent/`)

`create_deep_agent(model, durable, coordinator, cwd, ...)` is a factory that assembles a full deep-agent configuration in one call: task tools, a specialist subagent roster (researcher, planner, implementer), durable stores, a persistent `/memories` filesystem, and a deepened system prompt.

- **`coordinator=True`** — the parent agent strips heavy tools (`Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Read`); `COORDINATOR_SYSTEM_PROMPT` is injected via `SystemPromptConfig`; `TaskStopTool` is registered on the coordinator. Worker subagents receive full tool access via `build_child_tools`.
- **`durable=True`** — wires `SqliteSessionStore` + `SqliteRunStore` + `CompositeFileBackend` with a `/memories` route to `SqliteFileBackend` so memory persists across restarts.

### Background workers and fork/continue

```mermaid
sequenceDiagram
    participant P as Parent loop
    participant ST as SubagentTool
    participant W as Worker (child session)
    participant N as pending_notifications

    P->>ST: call (run_in_background=true)
    ST->>W: asyncio.create_task(_bg_run)
    ST-->>P: ack — turn continues, not blocked
    W->>W: run to completion (retain=true → stays in agent._sessions)
    W->>N: append <task-notification>
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
- **`run_in_background=True`** on `SubagentTool`: spawns `asyncio.create_task(_bg_run())` and returns an acknowledgement immediately. On completion, the task appends a `<task-notification>` XML `Message` to `session.pending_notifications`.
- The loop drains `session.pending_notifications` at the top of each turn (`_drain_pending_notifications`), yielding each notification as a `UserEvent` before `ContextInjectionHook.build_context()` runs, so the model sees task-completion content before the next provider call.
- `SubagentContinueTool` resolves a worker by id or display name via `resolve_worker`, then calls `continue_subagent()`, which re-drives the live child session using the full prior `provider_view`.
- `TaskStopTool` cancels the background `asyncio.Task` and signals abort on the child session; the `WorkerHandle` remains in `session.workers` so the worker can be continued later.
- `session.abort()` and `agent.close()` both cancel all running background worker tasks. `agent.close()` additionally clears `agent._sessions`.

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
subagents via `wf.agent` / `wf.parallel` / `wf.pipeline` / `wf.phase`, with
`wf.budget` exposing the shared `RunBudget`.

- A host session parents every `wf.agent` run (each is a normal
  `run_subagent` child); child `SubagentEvent`s and `WorkflowEvent`s reach the
  host via the `on_event` callback.
- **Journal = the run event log.** With `Agent(run_store=...)` and a
  `run_id`, each `wf.agent` result persists as
  `WorkflowEvent(kind="agent_end")`. Re-invoking with the same `run_id` folds
  the stored events back into a `WorkflowJournal` and replays the unchanged
  call prefix (`kind="agent_replayed"`, no provider call). Calls are keyed by
  `sha256(subagent_type, prompt)` + per-key occurrence counter, so parallel
  fan-out replays safely and an edited prompt invalidates only that call.
- The workflow function must be deterministic (no random/time-based
  branching) for resume replay to be correct.

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
  each call by `sha256(subagent_type, prompt)`; nondeterministic branching would replay
  the wrong prefix, so determinism is the price of cheap, correct resume.

---

Back to the [architecture index](./README.md).
