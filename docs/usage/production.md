# Minimal production wiring

Use this checklist when embedding Linch in a service with durable runs and shutdown handling.

This page is the short version of embedding Linch inside a long-running async
service: persistence, streaming, human-in-the-loop, resume after a restart,
graceful shutdown, and blast-radius controls. Each section links to the page
with the full detail — this is the wiring checklist, not a re-explanation.

Linch is a **library**. Your service owns the process and HTTP lifecycle;
Linch owns agent execution. The optional `linch.worker` runtime dispatches
runs and recurring work across a worker fleet.

For a local database, follow the
[Docker Postgres setup](./quickstart.md#5-keep-state-when-the-process-restarts).
The host reads `LINCH_POSTGRES_DSN` and passes it to each store; the SDK does not
read this variable automatically.

## 1. Build the agent once, reuse it

An `Agent` holds configuration, registries, context, and live sessions —
construct it at startup and share it across requests that use that configuration.
Use separate agents when tenants need different provider, tool, or policy
configuration; per-run identity and dependencies can remain request-scoped.

```python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

dsn = "<Postgres DSN>"

agent = Agent(
    model="gpt-5",                         # the default provider is OpenAI
    session_store=PostgresSessionStore(dsn),  # conversation state
    run_store=PostgresRunStore(dsn),         # checkpoints and run events
    cwd="var/workspace",
)
```

- `session_store` persists conversation history; `run_store` persists per-run
  checkpoints and events (required for durable resume). The built-in durable
  adapters are Postgres; `InMemorySessionStore` and `InMemoryRunStore` are for
  tests and development. Install the Postgres adapters with the `[postgres]`
  extra.
- A virtual filesystem is disabled by default. If offloaded results or files
  must survive across sessions or processes, enable the filesystem feature and
  use `PostgresFileBackend` from [Filesystem](./filesystem.md).
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
tools, context pressure, cost, and recovery counters), build a
[`RunReport`](./events.md).

## 3. Human-in-the-loop, durably

When a tool needs approval Linch yields a `PermissionRequestEvent` before it
invokes the configured `can_use_tool` callback. The event is a notification, not
a response channel: after the consumer receives it, continuing the iterator
invokes the callback, which must resolve before tool execution. Have the callback
await your UI or policy service. With no callback, an ask is denied. Resolved
allow/deny decisions are checkpointed and replayed after an interrupted run
resumes. For durable runs, give the callback a stable
`resume_policy_id` (and, when needed, `resume_policy_version`) so the run
contract can be verified after a restart.

```python
from linch import PermissionRequestEvent

async for event in session.run(prompt):
    if isinstance(event, PermissionRequestEvent):
        # Notify the UI; can_use_tool owns the eventual allow/deny response.
        await publish_approval(event.requests, session_id=session.id,
                               run_id=session.active_run_id)
```

Permissions and durable decisions: [Tools](./tools.md#permissions) and the
permissions section of [Agent & session](./agent.md).

## 4. Resume after a restart

With a `run_store`, a nonterminal run interrupted by a deploy or crash resumes
from its last checkpoint after its current run contract is verified. Rebuild the
agent with the same durable stores and approval policy, then reattach the
original session by ID. Persisted permission decisions and tool completions
replay; an external side effect that happened before its completion record became
durable may execute again. Tool integrations must use `ctx.idempotency_key` to
reconcile this at-least-once boundary.

```python
session = await agent.session(id=session_id)  # reattach the original session
async for event in session.resume(run_id):
    await publish(event)
```

Resume requires current wire-version-3 state, a checkpoint, and a matching
execution contract. Terminal runs, runs without checkpoints, and pre-3.0,
missing-version, unknown-version, or contractless state are rejected; start a
new run. Built-in Postgres stores reject incompatible existing schemas without
upgrading them; use fresh databases. See the [3.0 migration guide](../migration-3.0.md).

## 5. Shut down without leaking work

On shutdown, `await agent.close()` quiesces sessions, aborts and drains their
owned workers/tools, then closes stores, providers, filesystem backends, and
hooks that expose cleanup. Hosts own OpenTelemetry providers: flush and shut
them down after their agents have closed. To stop a single in-flight run, call
`session.abort()`; cooperative provider calls and tools observe the run's abort
signal, and the run emits an `aborted` result after it unwinds.

```python
try:
    await serve_forever()
finally:
    await agent.close()
```

## 6. Bound the blast radius

These are mechanisms; you supply the policy.

- **Budget** — `RunOptions(budget=RunBudget(max_tokens=..., max_cost_usd=...))`
  blocks subsequent provider calls when the run or its shared subagent budget is
  exhausted. An already admitted call can still consume its reported usage. See
  [Agent & session](./agent.md).
- **Loop guard** — enabled by default (`max_identical_tool_calls=3`,
  `max_consecutive_failures=3`); it stops repeated identical tool calls or
  consecutive all-failed tool batches.
- **Permissions** — use `ToolRule`, `PathRule`, and `BashRule` with the
  appropriate permission mode; unresolved asks are denied unless a
  `can_use_tool` callback resolves them.
- **Timeouts & retries** — `tool_timeout_ms` sets the agent default; a tool can
  override it with `execution_timeout_ms`. `tool_retry` retries read tools and
  only explicitly retryable write/exec tools; aborts are never retried.
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
PgQueuer jobs. The stores and queue manager reuse the host's asyncpg pool within
each process; each process creates its own pool. Reserve one pool connection per
worker process for LISTEN and enough additional connections for queries and runs.

```python
from linch import Agent, Principal, Profile, empty_tools
from linch.worker import Worker, WorkerJob, enqueue_run, install

await install(pool)  # once at process startup; serializes concurrent installers
support = Profile(id="support", version="1", system_prompt="Help resolve product issues.")
principal = Principal(tenant=tenant_id, user=user_id, roles={"support"})
run_id = await enqueue_run(pool, session_id, prompt, queue="provider-a",
                           meta={"tenant_id": tenant_id}, profile=support, principal=principal)

async def agent_factory(job: WorkerJob) -> Agent:
    provider = await provider_for_tenant(job.run.meta["tenant_id"])
    return Agent(
        model="your-model", provider=provider, tools=empty_tools(),
        session_store=job.session_store,
        run_store=job.run_store,
    )

worker = Worker(pool, agent_factory, queues={"provider-a": 8, "premium": 4},
                profiles=(support,), lease_ttl_s=30, stall_timeout_s=60, poll_interval_s=1)
await worker.run()
```

The factory is async and returns a **fresh agent per job**. The host chooses the
tenant, provider, tools, policy and dependencies; reuse exactly `job.run_store`
and `job.session_store`. The worker owns these adapters for its whole lifetime,
verifies their schemas before dequeueing, and supplies the same borrowed `job.pool`
to every factory. Per-job adapters add churn and repeat schema initialization.
Agent teardown preserves these shared adapters and leaves the host pool open; the
host closes its real pool after workers have drained. If a pool is shared with
other application code, preserve that application's connection-reset and listener
requirements.

A worker reconnects after transient database or listener failure, with local
exponential jitter capped at two seconds. Each retry rebuilds the manager and
restores LISTEN on one fresh connection borrowed for serialized queue SQL;
run/session writes use the remaining host pool. The previous cycle fully drains
before its connection is closed, so delayed failure buffers cannot reconnect and
hold a nonterminal job.
The existing stale-job recovery then reclaims interrupted work. Authentication,
schema, and configuration failures still propagate. Startup and connection
acquisition are bounded to two seconds per attempt.

`stop()` also interrupts reconnect backoff and prevents another dequeue; pending
startup/acquisition finishes within its attempt bound. Cancelling `run()` signals
stop and waits for active callbacks and teardown, including repeated
cancellation, before propagating cancellation. Active jobs have no forced drain
deadline; hosts must keep the pool open until `run()` returns.

The borrowed pool caches successful schema verification for its fixed database
and search path. Additional stores on `job.pool` reuse that verification without
schema SQL. Keep its database, role, and search path stable; after changing them
or changing a schema, create a new pool/worker to verify again. Serialize different
runs targeting the same session at the API boundary; the run lease does not
serialize distinct runs in one session.

`enqueue_run` creates a `pending` run and its job in one transaction with
`dedupe_key="run:<run_id>"`. A caller-supplied run ID makes repeated submissions
permanent no-ops, including after the queue job is acknowledged. Reusing an ID
with a different session is rejected; the first submission's prompt and metadata
win. `enqueue_runs(pool, [RunRequest(...)])` batches queue inserts in one
transaction. The job JSON includes run/session IDs, prompt, optional queue and
the caller's W3C `traceparent`. The worker restores it before execution, so the
kernel's `invoke_agent` span joins the caller's trace.

`enqueue_run(..., profile=..., principal=...)` and the matching `RunRequest`
fields select the domain configuration and authorization identity. The pending
run metadata and job JSON persist only the profile reference (id, version,
content hash) and serialized principal. Configure `Worker(profiles=(...))` with
every released definition its jobs may need. Missing definitions and hash
mismatches fail the run with an actionable `ConfigError` before provider or tool
execution; definitions and live plugin objects are never serialized.

The worker passes its plain `PostgresRunStore` to the factory and starts pending
runs with `RunOptions(run_id=run_id, profile=resolved_profile, principal=principal)`.
Runs with a checkpoint use `session.resume(run_id, options)` instead; both paths
keep the store's claimed lease. Resume and takeover restore identity from the
stored RunContract, so a new deployment must retain the exact pinned profile
definition while its runs can resume. See [domain profiles](./profiles.md).

Each named run queue is a PgQueuer entrypoint with a database-enforced
`concurrency_limit` across the **whole fleet**. Configure the same limits on
all workers. `0` means unlimited; the default is one `default` run queue with
unlimited concurrency. Queues must be registered on a worker to execute.
These limits cap runs; use host limiters for provider request/token quotas.

**Capacity.** Size the database and pools from a deployment-specific benchmark;
the SDK does not promise linear worker scaling. Use direct/session pooling for
the worker's LISTEN connection; transaction pooling needs a separate notification
connection. Batch `enqueue_runs` for bursts, bound enqueuer concurrency, and
measure with the consumers and tools used in production.

The queue only dispatches. A job separately claims the run lease using the
worker ID. Renewal starts immediately, then repeats every **lease TTL / 3**.
Transient renewal failures retry within the last confirmed TTL; a hung renewal
is bounded by that same deadline. Successful claim/renew replies start a fresh
TTL budget, so waiting for a pool slot does not consume a newly acquired lease.
`LeaseLostError` or expiry without a successful renewal cancels the run task
without aborting its signal or writing a terminal status. Session work is
quiesced before lease release. Completed, failed and
aborted runs are acknowledged without invocation; every nonterminal exit,
including cancellation and ownership loss, is retried from its checkpoint.
Queue-callback cancellation stops the run task while shielding cleanup and the
terminal-state check, preventing PgQueuer from acknowledging unfinished work.
Factory and session setup failures atomically commit failed status, error
metadata, an `ErrorEvent`, and one final `ResultEvent`. On takeover, terminal
totals and usage completeness come from the durable model-call journal, including
receipts newer than the last checkpoint. A failed or fenced terminal write leaves
the job retryable.

An agent failure is a durable result, not a queue retry. Infrastructure errors
such as an unreachable store or a competing live lease use
`DatabaseRetryEntrypointExecutor` backoff; exhausted jobs are held for host
inspection rather than discarded.

PgQueuer 1.6 retries by queue job ID: it resets status to `queued`, increments
attempts and clears `queue_manager_id` without checking the old queue owner. A
stale worker's retry can therefore requeue a job another worker has re-picked.
The run lease still excludes concurrent execution of that run; retry claims
that meet a live owner back off again. Queue ownership alone does not fence run
writes. The worker acknowledges only a persisted terminal run.

On process death, queue and lease heartbeats both stop. PgQueuer re-picks the
stale `picked` job once its last queue heartbeat is older than
`stall_timeout_s`. The next dequeue also includes polling, database and scheduling
delays; this is not a wall-clock recovery deadline from process death. The
new worker claims the expired lease and resumes the checkpoint, verifying the
stored execution contract. Choose a stall timeout at least as long as the lease
TTL, with margin for scheduling/DB delays. Durable tool receipts replay; external
tool effects still need `ctx.idempotency_key` reconciliation. PgQueuer acknowledges
task cancellation, so a hard process kill is required to exercise actual orphan
recovery. `worker.stop()` stops picking and drains active jobs; let `run()` return
before closing the host pool.

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

Measure enqueue latency and throughput on the deployment host. `enqueue_runs`
batches run rows and queue inserts in one transaction, reducing commit and
notification overhead; bound enqueuer concurrency for bursts. PgQueuer
notifications wake dispatch but are hints, not the source of truth. Stream
readers should poll persisted `run_events` by run ID and sequence when they need
reliable replay.

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
