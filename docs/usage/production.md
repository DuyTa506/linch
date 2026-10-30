# Minimal production wiring

This page is the short version of embedding Linch inside a long-running async
service: persistence, streaming, human-in-the-loop, resume after a restart,
graceful shutdown, and blast-radius controls. Each section links to the page
with the full detail — this is the wiring checklist, not a re-explanation.

Linch is a **library**. Your service owns the process and HTTP lifecycle;
Linch owns agent execution. The optional `linch.worker` runtime dispatches
runs and recurring work across a worker fleet.

For a local database, follow the [Docker Postgres setup](./quickstart.md#5-keep-state-when-the-process-restarts).
The host reads `LINCH_POSTGRES_DSN` and passes it to each store; the SDK does not
read this variable automatically.

## 1. Build the agent once, reuse it

An `Agent` holds configuration and its own registries — construct it at startup
and share it across requests/tenants. There is no process-global mutable state,
so independent `Agent` instances run concurrently in one process (multi-tenant
safe).

```python
import os
from linch import Agent, PostgresRunStore, PostgresSessionStore

dsn = os.environ["LINCH_POSTGRES_DSN"]

agent = Agent(
    model="claude-opus-4-8",
    session_store=PostgresSessionStore(dsn),  # conversation state
    run_store=PostgresRunStore(dsn),              # checkpoints for resume
    cwd="var/workspace",
)
```

- `session_store` persists conversation state; `run_store` persists per-run
  checkpoints (required for resume). In-memory variants (`InMemorySessionStore`,
  `InMemoryRunStore`) are for tests. Postgres-backed stores ship under the
  `[postgres]` extra.
- See [Agent & session](./agent.md).
- For Postgres run persistence, optional worker leases, and stream reconnect by
  sequence, see [Run stores](./run-stores.md).

## 2. Stream events to your host

A run is an async generator of typed events. Forward them to your UI/log/queue
as they arrive — do not wait for the run to finish.

```python
from linch import ResultEvent

session = await agent.session()
async for event in session.run(user_prompt):
    await publish(event)            # your transport
    if isinstance(event, ResultEvent):
        final = event.final_text
```

See [Events](./events.md). For a compact post-run health surface (slow/failing
tools, context pressure, cost, recovery counters) build a
[`RunReport`](./events.md).

## 3. Human-in-the-loop, durably

When a tool needs approval the stream yields a `PermissionRequestEvent` and the
run pauses with a checkpoint already written. Persist the decision through your
own UI, then resume — the decision replays and the tool is not re-prompted.

```python
from linch import PermissionRequestEvent

async for event in session.run(prompt):
    if isinstance(event, PermissionRequestEvent):
        run_id = session.active_run_id     # capture before the loop ends
        # ... ask a human out-of-band, store allow/deny against run_id ...
```

Permissions and durable decisions: [Hooks](./hooks.md) and the permissions
section of [Agent & session](./agent.md).

## 4. Resume after a restart

With a `run_store`, a run interrupted by a deploy or crash resumes from its last
checkpoint after its version-3 run contract is verified. Permission decisions and
durably recorded tool completions replay; a side effect that happened before
its completion record became durable may execute again. Tool integrations must
use `ctx.idempotency_key` to reconcile this at-least-once boundary.

```python
async for event in session.resume(run_id):
    await publish(event)
```

Resume requires current wire-3 state and a verified execution contract.
Pre-3.0, missing-version, unknown-version, and contractless runs are rejected;
start a new run. Built-in Postgres stores reject incompatible
existing schemas without upgrading them; use fresh databases. See the
[3.0 migration guide](../migration-3.0.md).

## 5. Shut down without leaking work

On shutdown, `await agent.close()` cancels background workers/tools and closes
hooks. Hosts own OpenTelemetry providers: flush and shut them down after their
agents have closed. To stop a single in-flight run, call
`session.abort()` — a cooperative tool sees `ctx.signal` and the run ends with an
`aborted` result.

```python
try:
    await serve_forever()
finally:
    await agent.close()
```

## 6. Bound the blast radius

These are mechanisms; you supply the policy.

- **Budget** — `RunOptions(budget=RunBudget(max_tokens=..., max_cost_usd=...))`
  caps a run and every subagent it spawns. See [Agent & session](./agent.md).
- **Loop guard** — on by default; stops runaway tool/failure streaks.
- **Permissions** — path/bash/tool rules; default-deny where it matters.
- **Timeouts & retries** — `tool_timeout_ms`, `tool_retry` per tool.
- **Provider concurrency** — `Agent(max_provider_concurrency=N)`, or your own
  `Limiter` for per-model quotas or a token bucket. This is the proactive
  counterpart to retry: retry reacts to a 429, a limiter avoids earning one. It
  bounds provider calls, not `run()` calls — see [Agent & session](./agent.md)
  and [Extending](./extending.md).
- **Truncation recovery** — opt in with `truncation_recovery=` (off by default;
  Linch never escalates output caps implicitly). See [Agent & session](./agent.md).
- **Redaction** — attach a `RedactionHook` to scrub tool results / final answers
  with host-supplied patterns. See [Hooks](./hooks.md).

## 7. Recurring work

For cron/webhook/interval/CI-driven recurring work, drive
`LoopRunner.run_once()` from your host scheduler — see
[Outer loop runner](./loop-runner.md) and
[`examples/recipes/runner_recipes.py`](../../examples/recipes/runner_recipes.py).

## Scale out

Install `linch[worker]` and import `linch.worker` explicitly. Core `import linch`
never imports PgQueuer; worker names are absent from `linch.__all__`.

The topology is **API processes → one Postgres → N worker processes**. API
processes call `enqueue_run`; they do not run the model loop. Postgres holds
sessions, pending runs, checkpoints, sequenced events, schedule definitions and
PgQueuer jobs. All stores and PgQueuer share the host's asyncpg pool within each
process; each process creates its own pool. Reserve at least one pool connection
per worker for LISTEN and enough additional connections for queries and runs.

```python
from linch import Agent, empty_tools
from linch.sessions.postgres import PostgresSessionStore
from linch.worker import Worker, WorkerJob, enqueue_run, install

await install(pool)  # once at process startup; serializes concurrent installers
run_id = await enqueue_run(pool, session_id, prompt, queue="provider-a",
                           meta={"tenant_id": tenant_id})

async def agent_factory(job: WorkerJob) -> Agent:
    provider = await provider_for_tenant(job.run.meta["tenant_id"])
    return Agent(
        model="your-model", provider=provider, tools=empty_tools(),
        session_store=PostgresSessionStore("", pool=job.pool),
        run_store=job.run_store,
    )

worker = Worker(pool, agent_factory, queues={"provider-a": 8, "premium": 4},
                lease_ttl_s=30, stall_timeout_s=60, poll_interval_s=1)
await worker.run()
```

The factory is async and returns a **fresh agent per job**. The host chooses the
tenant, provider, tools, policy and dependencies; reuse exactly `job.run_store`
so every write carries the claimed fencing epoch. Use `job.pool` for job-scoped
stores: it borrows the shared pool, so the worker can close the agent and stores
after the run without closing the host's pool. The host closes its real pool
after workers have drained. Serialize different runs targeting the same session
at the API boundary; the run lease does not serialize distinct runs in one session.

`enqueue_run` creates a `pending` run and its job in one transaction with
`dedupe_key="run:<run_id>"`. A caller-supplied run ID makes repeated submissions
permanent no-ops, including after the queue job is acknowledged. Reusing an ID
with a different session is rejected; the first submission's prompt and metadata
win. `enqueue_runs(pool, [RunRequest(...)])` batches queue inserts in one
transaction. The job JSON includes run/session IDs, prompt, optional queue and
the caller's W3C `traceparent`. The worker restores it before execution, so the
kernel's `invoke_agent` span joins the caller's trace.

Each named run queue is a PgQueuer entrypoint with a database-enforced
`concurrency_limit` across the **whole fleet**. Configure the same limits on
all workers. `0` means unlimited; the default is one `default` run queue with
unlimited concurrency. Queues must be registered on a worker to execute.
These limits cap runs; use host limiters for provider request/token quotas.

The queue only dispatches. A job separately claims the D1 run lease using the
worker ID. Renewal starts immediately, then repeats every **lease TTL / 3**.
Transient renewal failures retry within the last confirmed TTL; a hung renewal
is bounded by that same deadline. Successful claim/renew replies start a fresh
TTL budget, so waiting for a pool slot does not consume a newly acquired lease.
`LeaseLostError` or expiry without a successful renewal cancels the run task without aborting its signal or writing a terminal
status. Session work is quiesced before lease release. Completed, failed and
aborted runs are acknowledged without invocation; every nonterminal exit,
including cancellation and ownership loss, is retried from its checkpoint.
Queue-callback cancellation stops the run task while shielding cleanup and the
terminal-state check, preventing PgQueuer from acknowledging unfinished work.
An agent failure is a durable result, not a queue retry. Infrastructure errors
such as an unreachable store or a competing live lease use
`DatabaseRetryEntrypointExecutor` backoff; exhausted jobs are held for host
inspection rather than discarded.

PgQueuer 1.6 retries by queue job ID: it resets status to `queued`, increments
attempts and clears `queue_manager_id` without checking the old queue owner. A
stale worker's retry can therefore requeue a job another worker has re-picked.
The D1 run lease still excludes concurrent execution of that run; retry claims
that meet a live owner back off again. Queue ownership alone does not fence run
writes. The worker acknowledges only a persisted terminal run.

On process death, queue and lease heartbeats both stop. PgQueuer re-picks the
stale `picked` job after `stall_timeout_s`, plus up to the polling interval. The
new worker claims the expired lease and resumes the checkpoint, verifying the
stored execution contract. Choose a stall timeout at least as long as the lease
TTL, with margin for scheduling/DB delays. Durable tool receipts replay; external
tool effects still need `ctx.idempotency_key` reconciliation. The Postgres test
kills worker A at `provider_pending`, recovers on B with a queue limit of one,
and checks that A's retained epoch cannot append events or change status.
PgQueuer acknowledges task cancellation, so a hard process kill is required to
exercise actual orphan recovery. `worker.stop()` stops picking and drains active
jobs; let `run()` return before closing the host pool.

### Recurring work on the queue

`create_schedule(pool, session_id, payload, cron="0 9 * * *")` or
`interval_s=3600` creates a schedule definition and its first deferred job
atomically. Cron uses croniter, five fields, UTC. Each fire transaction creates
one deterministic run ID, advances the definition and inserts the next deferred
job with `execute_after` and a dedupe key containing the fire time. Replaying a
fire is a no-op. Missed occurrences catch up in fire-time order after downtime.
`list_schedules(pool, session_id=...)` returns definitions;
`cancel_schedule(pool, id, session_id=...)` disables future fires while preserving
runs already materialized. API processes need no scheduler polling loop.

Hosts mount the model tools explicitly:

```python
from linch.worker import register_schedule_tools

undo = register_schedule_tools(agent.tools, pool, session_id=session_id)
# CreateSchedule / ListSchedules / CancelSchedule are bound to this target.
await undo()  # identity-safe, idempotent Disposable
```

### Stream from any process by sequence

Stream readers need only a `PostgresRunStore`, not the worker's memory. Read
`await store.load_events(run_id, after_seq=last_seq)`, deliver in ascending order,
and advance `last_seq` only after successful delivery. Persist that cursor in the
client (for example SSE `Last-Event-ID`) and use it on reconnect. A sequence is
local to a run. Poll `run_events` by `(run_id, seq)` when no wakeup channel is
available or NOTIFY contention is material; notifications are hints, never the
source of truth. An empty poll does not mean completion; observe the persisted
result event. The runnable [fleet example](../../examples/worker/minimal.py)
includes enqueue, worker and polling-reader modes without a live model key.

### Enqueue / NOTIFY measurement

Measured 2026-10-31 with Python 3.11.14, PgQueuer 1.6.0 and local Postgres 16.2
via Unix socket. Each result is the median of three 3,072-run samples with a
warm 33-connection pool, one separate active LISTEN receiver, short prompts
and no consumers. Time includes run-row
writes, queue inserts, transaction commit and PgQueuer's NOTIFY trigger; it does
not isolate the NOTIFY lock from database/network/client work. Batches contain
32 runs and use one transaction and one queue insert per batch.

| Concurrent enqueuers | Single runs/s | Batch runs/s (32) |
|---:|---:|---:|
| 1 | 1,103.0 | 8,199.2 |
| 8 | 2,471.7 | 17,700.4 |
| 32 | 2,375.0 | 18,788.8 |

Reproduce with `PYTHONPATH=src python examples/worker/benchmark_enqueue.py`
and `AGENT_KIT_TEST_PG_DSN` (or `LINCH_POSTGRES_DSN`). The script creates and
removes an isolated schema. Single-submit throughput stopped improving beyond
eight enqueuers on this host. Postgres NOTIFY serializes commit work through a
global lock; batch enqueue amortizes commits and notifications. Prefer batches
for bursts, bound enqueuer concurrency, and remeasure with your workload and
consumers. For stream readers, polling `run_events` by sequence avoids adding
per-event NOTIFY traffic to that shared commit path.

## Optional dependencies are opt-in

The core install includes the OpenAI SDK, PyYAML, `typing_extensions`, and
`jsonschema`. Configured structured-output schemas are always validated.
Integration dependencies are extras and fail with an explicit
`pip install 'linch[...]'` message when missing:

| Extra | Enables |
|---|---|
| `linch[anthropic]` | Anthropic provider |
| `linch[gemini]` | Gemini provider |
| `linch[mcp]` | MCP tool servers (requires `mcp>=2.0.0`; 1.x is not supported) |
| `linch[worker]` | PgQueuer dispatch, asyncpg, and croniter schedules |
| `linch[postgres]` | Postgres session/run stores and filesystem backends |

---

## Related pages

- [Agent & session](./agent.md) · [Events](./events.md) · [Hooks](./hooks.md)
- [Outer loop runner](./loop-runner.md) · [Coordination](./coordination.md)
- [Extending](./extending.md) · [Providers](./providers.md)
