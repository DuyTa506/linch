# System Overview

> Use this page when you need the high-level runtime topology, host boundary,
> and durable data flow.

> Part of the [Linch architecture guide](./README.md).

Linch 3.0 is a headless, async agent kernel. It is embedded in a host service;
the host owns HTTP, authentication, tenant routing, and the OpenTelemetry SDK.
Linch owns the provider-agnostic run loop, tool execution, permissions,
session projections, durable run state, and extension seams. There is no UI in
the kernel.

The supported Python surface is `linch.__all__`. `linch.ext.*` packages are
explicit extensions and are not imported by the core package. `linch.worker` is
also explicit because it loads the optional PgQueuer runtime.

## Runtime shape

For an embedded single-process use, the host creates an `Agent`, obtains a
`Session`, and consumes the typed event stream from `Session.run()` or
`Session.resume()`. For a fleet, API processes enqueue runs and worker
processes execute them. Postgres is the durable source of truth for sessions,
runs, checkpoints, leases, and sequenced run events.

```mermaid
flowchart LR
    subgraph Host["Host service"]
        API["HTTP/API process"]
        OTel["OpenTelemetry SDK/exporter"]
        Deps["RunOptions(deps=...)\ntenant services"]
    end

    subgraph Dispatch["Optional linch.worker"]
        Queue["PgQueuer"]
        Worker["Worker\nclaim / renew / release"]
    end

    subgraph Kernel["Linch headless kernel"]
        Agent["Agent\nContext + registries"]
        Session["Session\nappend-only SessionLog"]
        Loop["loop/runner.py\nturn phases"]
        Guard["turn_stopping\nreliability listeners"]
        Scheduler["scheduler.py\npermission + tool execution"]
        Provider["providers/*\nnormalized provider stream"]
    end

    subgraph Durable["Postgres source of truth"]
        Sessions["PostgresSessionStore"]
        Runs["PostgresRunStore\nruns + leases + checkpoints"]
        Events["run_events\n(run_id, seq)"]
    end

    API -->|"enqueue_run"| Queue
    Queue --> Worker
    Worker -->|"RunLease(epoch)"| Runs
    Worker --> Agent
    API -->|"embedded run"| Agent
    Agent --> Session --> Loop
    Loop --> Provider
    Loop --> Scheduler
    Loop --> Guard
    Scheduler --> Deps
    Session <-->|"messages / snapshots"| Sessions
    Loop <-->|"checkpoint before side effects"| Runs
    Runs --> Events
    Events -->|"load_events(after_seq=...)"| API
    Agent -.->|"tracer + meter resolved from Context"| OTel
```

## Boundaries and invariants

- `Agent` is configuration and scoped services. It owns its per-agent
  `Context`; when a host `Context` is supplied, the agent's scope is a child
  and the host parent remains host-owned. `Session` is
  conversation and run state. `SessionLog` is the append-only source for the
  session-owned `provider_view` and `full_history` projections; request-scoped
  context is assembled into `ProviderRequest` and is not appended to the log.
- The run loop is async. Provider and store adapters are async protocols, and
  blocking synchronous tool functions are moved off the event loop by the
  `FunctionTool` adapter.
- A leased run has at most one live worker owner. `RunLease.epoch` is a fencing token.
  `claim()` increments the epoch when ownership changes; `renew()` keeps the
  same epoch; a write with a stale epoch raises `LeaseLostError`.
- The loop writes a checkpoint before an external operation whose result must
  be recoverable. `provider_pending` is saved before a provider stream, and
  `tool_batch_pending`/`tool_executing` are saved before tool execution. Stores
  can commit pending run events and the checkpoint together through
  `save_checkpoint_with_events()`.
- Durable run events have a per-run sequence. A reader reconnects with
  `PostgresRunStore.load_events(run_id, after_seq=...)`; notifications are
  wakeups, not the source of truth.
- Durable database state is Postgres. `InMemorySessionStore` and
  `InMemoryRunStore` are process-local implementations for tests and
  development.
- OpenTelemetry is the observability API. Without a host-configured SDK, the
  API is a no-op. Tracers and meters are resolved through the agent's
  `Context`, so hosts and tests can provide scoped providers without mutable
  process-global Linch state.
- `Agent` defaults to `ReliabilityPolicy()`; its listener runs on the
  `loop/turn_stopping.py` dispatcher and can request bounded correction turns
  for invalid provider responses. Pass `reliability=None` to disable it.

## Host wiring

The durable stores have separate import paths because Postgres is optional:

```python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

dsn = "postgresql://user:password@localhost/linch"
agent = Agent(
    model="your-model",
    session_store=PostgresSessionStore(dsn),
    run_store=PostgresRunStore(dsn),
)
```

Install the Postgres extra before constructing those stores. For a fleet,
install `linch[worker]` and import the worker runtime explicitly:

```python
from linch.worker import Worker, enqueue_run, install

await install(pool)
run_id = await enqueue_run(pool, session_id, prompt, queue="default")
worker = Worker(pool, agent_factory, queues={"default": 8})
await worker.run()
```

`agent_factory` receives a `WorkerJob`; it must reuse `job.run_store` and
`job.session_store`. `Worker` claims the run lease, renews it while the run is
active, resumes a checkpoint after a takeover, and acknowledges a queue job
only after the run is terminal. It reconnects after transient Postgres or
listener failures and leaves nonterminal jobs retryable.

## Extensions

The kernel stays small. Workflow, evaluation, deep-agent presets, reports,
the recurring loop runner, and the scaffolding console are under explicit
`linch.ext.*` modules:

- `linch.ext.workflow` — journaled deterministic workflows;
- `linch.ext.evals` — scripted providers, cases, and scorers;
- `linch.ext.deep_agent` — coordinator/worker presets;
- `linch.ext.reports` — run-report helpers;
- `linch.ext.loop_runner` — host-driven recurring loops; and
- `linch.ext.scaffold` — the `linch` project-scaffolding console entry point.

These modules may use the kernel, but importing `linch` does not import them.

Back to the [architecture index](./README.md).
