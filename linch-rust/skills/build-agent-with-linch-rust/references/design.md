# Design an agent system on linch-rust

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

Ask at most five questions, about needs rather than linch-rust names. Give your recommended answer
with each one. Skip any question the request already answers, and record your assumption instead.

| Decision | Ask the user | What the answer changes |
|---|---|---|
| Control | Are the steps known in advance, or must the model work them out? | Shape, section 3 |
| Failure | If it dies halfway, may it start over? What must never happen twice? | Stores, run ids, idempotent host tools |
| Risk | Which actions need a person's approval first? | Scopes, permission rules, approvals |
| Knowledge | Where does the data live, how large is it, how fresh must it be? | Host tools, MCP, `context_build` callbacks |
| Output | Who consumes the result, a person or another program? | Streaming, what the host parses |
| Limits | What is the budget per run, and how long may it take? | `max_turns`, timeouts, model choice |
| Load | How often does it run, how many at once, how many tenants? | Sidecars, a shared server, pools |
| Proof | What would make you say it works? | Tests and a task set, section 5 |

## 3. Choose the shape

Take the first row that fits. Move down only for a reason you can state.

| Situation | Shape | Read |
|---|---|---|
| The model picks tools until the job is done | One agent with host tools | `usage/agent.md`, `usage/tools.md` |
| Known steps, one after another | Your Python code calls `agent.run` once per step, each with a `run_id` you choose | `usage/runs.md` |
| Two roles on the same provider and stores | Two agents whose configs differ only in `profile` | `usage/profiles.md` |
| The model hands focused tasks to helpers it picks | `subagents`, with definitions in `<config_dir>/agents/` | `usage/subagents.md` |
| Independent subtasks the model runs side by side | `subagents` with `workers: {"background": True}` | `usage/subagents.md` |
| Subtasks cannot be known in advance; plan, delegate, keep notes | `deep_agent` | `usage/deep-agent.md` |
| Sessions of one agent exchange messages | `mailbox` | `usage/subagents.md` |
| Many processes, machines or tenants share one kernel | `linch-server --config` and `Server.connect` | `usage/server.md` |

Tradeoffs to keep in view:

- The Python client has no workflow engine, eval harness or job queue. Those are Rust crates
  (`architecture/ext.md`, `architecture/worker.md`). For known steps your own code owns the order,
  and nothing journals it: each step is a durable run on Postgres, so record which steps finished
  and make each one safe to run twice. A queue or a cron trigger is the host's, calling
  `agent.run` and `agent.resume`.
- A deep agent means the model owns the order, which is flexible but not deterministic, and
  nothing records the orchestration beyond the runs and sessions. Its built-in profiles are
  durable and need Postgres stores.
- Subagents cost tokens and add failure paths. Use them for context isolation, different tools,
  or real parallelism, not to mirror a human team. Compare against one agent when you are unsure.
- A child run cannot suspend for approval: a call that would ask is denied unless a permission
  rule or a `permission_request` callback allows it. Grant the child's tools before you rely on it.
- A run waits for its background workers before it ends, and runs at most
  `workers.max_background` of them at once (4 unless set).
- Tool calls run in parallel only for read-scoped work. Write and exec tools stay serial, and a
  subagent runs beside others only when every tool it lists is read-scoped.
- The mailbox lives in the server process. Its messages are lost when the server stops, even on
  Postgres.

## 4. Settle the design points

**Durability.** Memory stores are for tests and development: runs live in the agent handle and
are gone with the server. If a run must survive a restart, or be resumed or approved from another
process, use Postgres stores (`usage/stores.md`). On resume, a tool whose start was recorded is
never run again, and a model call that was sent but not recorded may be sent twice
(`usage/runs.md`). So make host tools that change things idempotent with
`ToolContext.idempotency_key`, or check before acting. Choose run ids yourself so a crash before
`agent.run` returns can still find the run. Keep the config, tools, callbacks and skill files of
every released version until its runs finish, because a different contract cannot resume them.

**Human approval.** Give each tool the narrowest scope. In mode `default`, write and exec calls
suspend the run; answer later with `agent.approve`, from another process on Postgres. An approve
sent twice ends with `NotSuspended`, so continue with `agent.resume`. To decide in code without
suspending, declare a `permission_request` callback. Rules in `permissions.layers` allow, ask or
deny by tool, path or shell command, and can match the roles of the run's `principal`
(`usage/tools.md`).

**Blast radius.** Set `spec.max_turns`, `spec.call_timeout_ms` and each host tool's `timeout`.
Give a workspace only to agents that need files, and turn off `Bash` with `bash: False` unless
the job needs commands. Deny what must never run with a deny rule or a profile's `tool_deny`,
which no mode overrides. Cost is priced only for the models in the built-in table
(`usage/events.md`); for others, track tokens from `UsageEvent` yourself.

**Tenants.** Give each tenant or product area its own `namespace`, since stores are keyed by it.
Pass a `principal` to every `run`, `resume` and `approve` that acts for someone. A shared
`linch-server --config` adds per-tenant tokens, allowlists for providers, paths, DSNs and MCP
commands, and caps on runs, agents and connections (`usage/server.md`). Keys stay in the
server's environment.

**Context.** Prefer host tools, MCP servers or a `context_build` callback that adds retrieved
blocks over stuffing data into the prompt (`usage/agent.md`). Long sessions need
`spec.compaction`; large tool results need `filesystem` offload; a large tool catalog needs
`tool_search`.

**Load.** Decide where the kernel runs: one `Server()` sidecar per process, several sidecars
with sessions spread across them, or one shared server. Size `stores.max_connections` at about
two connections per run in flight per server process. See step 6 of `SKILL.md`.

**Tracing.** `linch_rust.otel` builds OpenTelemetry spans and metrics from each run's events,
and the host supplies the providers and exporter (`usage/observability.md`). Decide now:

- Which backend receives traces. Any OTLP collector works.
- Whether prompts and tool content may be captured. They are left out unless
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `true`, and turning that on can put user
  data in the trace backend.
- How a run joins your request's trace: pass `trace_id=` to `agent.run`, and log `run.id` and
  `run.trace_id`.
- Which signals you will watch: cost, latency, failed tools, and runs stopped by `max_turns`.

## 5. Decide how you will prove it

Before writing code, list:

- The failure each part exists to handle, such as a crash mid-run, a denied action, an empty
  retrieval, or a run that hits `max_turns`. Each one becomes a test under step 4 of `SKILL.md`.
- A small set of real tasks with checkable results. The client has no eval harness, so run them
  as pytest cases: scripted turns for the plumbing, and a live suite, skipped without
  credentials, for the model. Run it on every prompt, model, or architecture change, and keep a
  one-agent baseline when you chose something more complex.

## 6. Write it down and get approval

Keep the design to about one page, and save it where the project keeps such notes:

```markdown
# <system name>

Goal: <what it does, for whom>
Shape: <choice from section 3, in one sentence>
Why: <the need that requires this shape>
Rejected: <the simpler option, and what it could not do>

Flow: <short diagram or numbered steps>
State: <stores, run ids, what happens on restart>
Approval: <actions a person must approve, and who>
Limits: <max_turns, timeouts, concurrency>
Placement: <sidecars or a shared server, tenants, pool size>
Tracing: <backend, content capture, signals>
Proof: <failure tests, task set, pass bar>
Assumptions: <what you decided without asking>
```

Show it to the user and wait for approval, because this is the cheapest point to change direction.
Then build in the smallest slice that runs end to end, and follow `SKILL.md` from step 1.
