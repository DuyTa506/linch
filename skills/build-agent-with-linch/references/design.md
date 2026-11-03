# Design an agent system on Linch

Use this before building when the wrong architecture would be expensive to undo. The result is a
short design the user approves, not a long document.

Paths are relative to `$DOCS` from `SKILL.md`. Those pages hold the APIs. This page only helps you
choose, so check the page before relying on a name here.

## 1. Learn what you can alone

Read the project, its data, its existing services, and the user's request. Do not ask anything you
can find out yourself.

Start by asking whether this needs an agent at all. Fixed logic belongs in plain code, and one
model call with good context may be enough. Each addition below must solve a problem you can name.

## 2. Ask only what changes the architecture

Ask at most five questions, about needs rather than Linch names. Give your recommended answer with
each one. Skip any question the request already answers, and record your assumption instead.

| Decision | Ask the user | What the answer changes |
|---|---|---|
| Control | Are the steps known in advance, or must the model work them out? | Shape, section 3 |
| Failure | If it dies halfway, may it start over? What must never happen twice? | Stores, journaling, idempotent side effects |
| Risk | Which actions need a person's approval first? | Permissions, interrupts |
| Knowledge | Where does the data live, how large is it, how fresh must it be? | RAG, memory, tools |
| Output | Who consumes the result, a person or another program? | Streaming, structured output |
| Limits | What is the budget per run, and how long may it take? | Budgets, model choice, concurrency |
| Load | How often does it run, how many at once, how many tenants? | Workers, recurring work, per-run principals |
| Proof | What would make you say it works? | Evals, section 5 |

## 3. Choose the shape

Take the first row that fits. Move down only for a reason you can state.

| Situation | Shape | Read |
|---|---|---|
| The model picks tools until the job is done | One `Agent` | `docs/usage/agent.md`, `docs/usage/tools.md` |
| Known steps, one after another | `run_workflow` with `wf.agent` and `wf.step` | `docs/usage/workflows.md` |
| Known categories that need different handling | Workflow that branches on a classification result | `docs/usage/workflows.md` |
| Independent subtasks, or several attempts to compare | `wf.parallel`, or `wf.settled` when partial results are useful | `docs/usage/workflows.md` |
| Many items through the same stages | `wf.pipeline` | `docs/usage/workflows.md` |
| Output must meet clear criteria that feedback improves | Workflow loop of generator and checker, with a round limit | `docs/usage/workflows.md` |
| Subtasks cannot be known in advance | `create_deep_agent` or coordinator mode | `docs/usage/deep-agent.md` |
| Agents exchange messages across turns | Mailbox, which is in-process | `docs/usage/coordination.md` |
| A trigger starts the work repeatedly | `LoopRunner.run_once()` from the host | `docs/usage/loop-runner.md` |

Tradeoffs to keep in view:

- Shape and placement are separate. `linch.worker` is a queue of processes, not an agent pattern.
  It decides where a queued prompt runs, on any shape built from an `Agent`, and adds fleet
  limits and crash takeover. See "Scale out" in `docs/usage/production.md`.
- `linch.worker` does not run `run_workflow`. For a workflow, the host picks the process and
  re-invokes unfinished runs. See "Running across processes" in `docs/usage/workflows.md`.
- A worker job closes its session when the run ends, which cancels unfinished background
  subagents. Use foreground subagents in a job.
- A workflow means your code owns the order, and durable runs can replay it. A deep agent means the
  model owns the order, which is flexible but has no workflow journal. If replaying the
  orchestration is a requirement, choose the workflow.
- Subagents cost tokens and add failure paths. Use them for context isolation, different tools, or
  real parallelism, and not to mirror a human team. Compare against one agent when you are unsure.
- Agent tool calls run in parallel only for read-scoped work. Write and exec tools stay serial. See
  `docs/usage/tools.md`.

## 4. Settle the design points

**Durability.** In-memory stores are for tests and development. If a run must survive a restart,
use the Postgres stores (`docs/usage/run-stores.md`, `docs/usage/production.md`). In a workflow,
only `wf.agent` and `wf.step` replay, so put every side effect in a `wf.step` and make it
idempotent, because a crash can land before the journal write. Keep control flow deterministic.

**Human approval.** Inside a workflow, `wf.interrupt` parks the run until the answer arrives. For
single tool calls, use permission rules with a `can_use_tool` callback. An unresolved ask is
denied, so a missing callback blocks the tool.

**Blast radius.** Set a run budget, tool timeouts, narrow permissions, and provider concurrency.
Keep the default loop guard. See "Bound the blast radius" in `docs/usage/production.md`. Give each
agent only the tools its job needs.

**Context.** Prefer tools or retrieval over stuffing data into the prompt. See
`docs/usage/context-and-memory.md`. Long-running agents also need a view on compaction, in
`docs/usage/agent.md`, and result offload, in `docs/usage/filesystem.md`.

**Tracing.** Linch emits OpenTelemetry spans and metrics, and the host supplies the provider and
exporter, as described in `docs/usage/events.md`. Decide these now, because they are hard to
retrofit:

- Which backend receives traces. Any OTLP collector works.
- Whether prompts and tool content may be captured. They are omitted by default, and turning them
  on can put user data in the trace backend.
- How a trace crosses process boundaries. Carry `traceparent` in the job payload.
- Which signals you will watch: cost, latency, failed tools, and runs stopped by a guard or budget.

## 5. Decide how you will prove it

Before writing code, list:

- The failure each part exists to handle, such as a crash mid-run, a denied action, an empty
  retrieval, or a spent budget. Each one becomes a test under step 4 of `SKILL.md`.
- A small eval set of real tasks with checkable results, using `docs/usage/evals.md`. Run it on
  every prompt, model, or architecture change, and keep a one-agent baseline when you chose
  something more complex.

## 6. Write it down and get approval

Keep the design to about one page, and save it where the project keeps such notes:

```markdown
# <system name>

Goal: <what it does, for whom>
Shape: <choice from section 3, in one sentence>
Why: <the need that requires this shape>
Rejected: <the simpler option, and what it could not do>

Flow: <short diagram or numbered steps>
State: <what is durable, what happens on restart>
Approval: <actions a person must approve>
Limits: <budget, timeouts, concurrency>
Tracing: <backend, content capture, signals>
Proof: <failure tests, eval set, pass bar>
Assumptions: <what you decided without asking>
```

Show it to the user and wait for approval, because this is the cheapest point to change direction.
Then build in the smallest slice that runs end to end, and follow `SKILL.md` from step 1.
