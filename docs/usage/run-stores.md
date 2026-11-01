# Durable run stores

A `RunStore` persists run checkpoints, status, and an ordered event log. Session
history is stored separately by the session store. Pass both stores to `Agent`
when conversation history and resumable runs must survive a restart.

| Store | Lifetime | Deployment |
|---|---|---|
| `InMemoryRunStore` | Process memory | Tests and local experiments |
| `PostgresRunStore(dsn)` | Postgres database | Local durability and multiple host workers |

Install the Postgres backend with `pip install 'linch[postgres]'`. Importing
`PostgresRunStore` does not import asyncpg; constructing it checks the optional
dependency and gives an install hint when it is missing. Connections and tables
are initialized on first use. The existing `[postgres]` extra covers session,
virtual-file, and run stores.

```python
from linch import Agent, PostgresRunStore
from linch.sessions.postgres import PostgresSessionStore

runs = PostgresRunStore("postgresql://user:password@localhost/linch")
sessions = PostgresSessionStore("postgresql://user:password@localhost/linch")
agent = Agent(model="gpt-5", session_store=sessions, run_store=runs)
```

Close stores with `await store.close()` when their host shuts down.
`PostgresRunStore` accepts `pool=`, `min_size=`, and `max_size=` like the
Postgres session store. A supplied pool belongs to its host; stores never close
it, including after initialization failure. The host closes it after all stores
using it have stopped.

Store-owned pools use asyncpg 0.32's `reset=` hook. The driver performs its
internal rollback/cancellation cleanup on release, and the Linch callback
omits the extra session-reset SQL round trip. Hosts that supply a pool should
use the same callback when they want the same hot-path behavior:

```python
async def reset_connection(_connection):
    return None

pool = await asyncpg.create_pool(dsn, reset=reset_connection)
runs = PostgresRunStore(dsn, pool=pool)
```

The callback runs after asyncpg's internal cleanup; it must not replace that
cleanup with a connection subclass. The no-op form also omits asyncpg's
additional `SET`/GUC, advisory-lock, cursor, and `LISTEN` reset SQL. Use a
dedicated pool for Linch, or add the host's required session-state cleanup to
its callback when sharing a pool with other workloads.

## Atomic phase writes

`create_run(session_id, *, id=None, meta=None, initial_checkpoint=None)` must
commit the supplied initial checkpoint with a fresh running row or the fenced
adoption of a pending row. The SDK always supplies a `started` checkpoint, so
worker-owned running runs are resumable even before startup returns. The optional
default supports direct store callers; it does not make manually created runs
without a checkpoint resumable. Custom stores must accept the new keyword.

Startup events commit with the checkpoint that owns their progress. Ordinary
runs use `user_appended`; startup-affecting hooks also have a preparation boundary
that freezes the prompt, remaining hook events, and post-hook history baseline
before the SDK appends its prompt. This distinguishes hook context from the
prompt even when their messages are identical. Hook-free runs need no extra
checkpoint. The initial checkpoint captures hook state before `AgentStart`;
startup accounting writes preserve that snapshot until preparation completes.
Startup hooks must be repeatable across takeover; arbitrary external hook side
effects do not have an exactly-once guarantee.

Every `RunStore` implements
`save_checkpoint_with_events(run_id, checkpoint, events, *, status="running")`.
The loop uses it whenever a phase has pending events, committing those events
and its checkpoint in one fenced statement before a guarded tool runs. It returns
`(run_record, event_seqs)`.

Terminal writes follow the same atomic boundary.
`mark_failed(run_id, checkpoint=None, error=None, *, events=())` commits the
failed status, checkpoint, error metadata, and pending events together and
returns `(run_record, event_seqs)`. Success, interruption, and abort commit
their terminal checkpoint and events with `save_checkpoint_with_events`.

The statement fences the run and atomically advances its sequence counter,
then inserts the events through a data-modifying CTE. Encoding, writing, or
fencing failure changes neither the checkpoint nor the events; the loop
retains its pending events.

## Optional leases and fencing

Both built-in stores implement the optional `RunLeaseStore` capability.
Custom stores may omit leasing when the host manages run ownership itself.
Hosts can detect lease support with `getattr(store, "claim", None)`.

```python
from linch import RunLeaseConflictError

async def handle_job():
    run = await runs.create_run("session-123", id="job-456")
    try:
        lease = await runs.claim(run.id, owner="worker-unique-id", ttl_s=30.0)
    except RunLeaseConflictError:
        # Another worker owns the live lease; leave the job for that worker.
        return

    try:
        # The host schedules heartbeats while it drives or resumes the run.
        lease = await runs.renew(lease)
    finally:
        # Stop all work for this run before ending ownership.
        await runs.release(lease)
```

`RunLease` is immutable and carries `run_id`, `owner`, `epoch`, and
`expires_at` (Unix seconds). TTL must be positive and finite. Claiming an unknown
run raises `KeyError`; create the run first. Use a unique owner identifier for
each independent worker. Claiming an already live lease with the same owner
preserves the epoch and refreshes the TTL. Renew uses the TTL from that store
instance's most recent claim and requires its matching live lease.

Each instance remembers the epoch it claimed for each run. Checkpoint saves,
event appends (including batches), status updates, and model-input snapshot
writes check that epoch in the same statement as the write. If another worker
claims an expired lease, the epoch increases and the previous holder's next
write raises `LeaseLostError`. The failed write changes no persisted state.
Expiry alone permits takeover; writes are fenced by epoch, not by a local
clock check. An expired lease cannot be renewed.

Release clears ownership and advances the epoch when the lease still matches.
Every release attempt removes this instance's holder-local state only when its
owner and epoch still match the released lease, including when it raises
`LeaseLostError` after a takeover. A newer claim on the same instance retains
its local state. Releasing a stale lease does not affect the current owner. A fenced write retains its local
lease state until the host calls release, so further zombie writes stay fenced.

Hosts call release in `finally` after stopping all work for the run. Release
ends fencing for that run on this instance; do not write to it after releasing
it. The local ownership maps therefore track active ownership rather than
growing with every run a long-lived worker has ever claimed.

Worker reconnect cycles retain these adapters and ownership maps; they drain
the old manager before rebuilding queue dispatch on a fresh connection. The host
pool remains open throughout recovery and graceful shutdown.

The host owns claiming, renewal, and stopping work after lease loss; the agent
loop does not start a heartbeat. A lease fences the run store's writes. Host
tools still need their own idempotency or fencing for external effects.
Instances that never claimed a particular run retain their existing write
behavior, including when another instance has a lease. All competing host
workers must opt into claiming for ownership protection.

Built-in Postgres stores create complete version-3 schemas,
including run lease columns, on fresh databases. Existing databases with old,
missing, or unknown schema metadata are rejected without alteration; use fresh
databases and start new runs. There is no automatic schema upgrade or legacy
resume option. Persisted envelopes likewise require wire version 3; see the
[3.0 migration guide](../migration-3.0.md).

The run-row counters serialize per-run event sequence allocation across store
instances without a `MAX(seq)` or `FOR UPDATE` hot-path read. Asyncpg performs
database I/O asynchronously.

## Failure semantics

During an active resumable run, transient connection failures reaching the loop's
recovery boundary interrupt the attempt without terminal events or a failed status. The loop stops
background work and skips buffered writes and snapshot cleanup, preserving the
last durable checkpoint for worker retry or lease takeover. This covers connection
and timeout errors, network-unreachable/down errors, asyncpg connection and server
availability errors, closed/closing asyncpg connection or pool errors, and explicit
causes wrapped by a store. Permanent OS, configuration, and query-argument errors
keep their ordinary failure behavior. Provider/tool
errors and agents without a run store keep their ordinary failure behavior.

The store calls inside `Session.notify` (durable inbox), `Session.append`, and
`Session.update_meta` use the same transient-error classification during an
active durable run. Their immediate caller receives the recovery exception;
hook and tool adapters retain their own exception handling.

After a run has been created, a non-transient session-store or run-store write
failure ends the iterator with an `ErrorEvent` carrying the exception's class name and
retryability, followed by a `ResultEvent(subtype="error")`. The loop attempts
terminal persistence once; if that also fails, it logs the secondary failure
with the run ID and keeps the terminal events available to the caller.
Persistence is not retried by the loop, so the host must reconcile a run whose
store stayed unavailable.

If `create_run` fails, iteration raises that same store exception before the
loop starts. No terminal events or `AgentStop` are emitted for a run that was
never created.

Optional provider-view snapshot caching and model-input snapshot deletion or
pruning are best-effort: ordinary failures are logged and the run continues.
Saving model-input snapshots remains required: non-transient failures end the
run with the typed error pair above; transient connection failures interrupt it
for recovery from the durable checkpoint.

`LeaseLostError` means another holder owns the run. The loop cancels background
workers and stops writing immediately, including terminal persistence, buffered
event flushing, and snapshot cleanup. The caller receives an in-memory
`ErrorEvent` named `LeaseLostError`, then a
`ResultEvent(subtype="error", stop_reason="lease_lost")`. Those events are not
in the persisted reconnect log. `AgentStop` fires once for either failure.

Resuming a terminal run, or a run without a checkpoint, raises the public
`RunNotResumableError`. Its `run_id` and `status` identify the rejected run.
Prompt-cache advisories are per-process diagnostics; resume restarts the comparison,
so the first post-resume request may lack an advisory an uninterrupted run would emit.

The host owns lease renewal and releases the lease in `finally`, after all run
work has stopped. A stale release can itself raise `LeaseLostError`; it cannot
alter the current owner's run. If retention needs reconciliation, the host also
owns snapshot cleanup for failed or closed runs.

## Reconnect using the persisted event sequence

Every persisted event has a per-run monotonically increasing, 1-based `seq`.
Single-event and batch appends share the same sequence. Save the last sequence
successfully delivered to your client, then read only later events:

```python
cursor = last_delivered_seq  # 0 for a new connection
for row in await runs.load_events(run_id, after_seq=cursor):
    await publish({"seq": row.seq, "event": row.event})
    cursor = row.seq
```

Rows arrive in ascending sequence order and `after_seq` is exclusive. Persist
the cursor after delivery; clients can deduplicate by `(run_id, seq)` when a
disconnect makes acknowledgment uncertain. The event log contains persisted
events, including private recovery facts that are never yielded by the run
iterator; those facts still consume sequence numbers and require the matching
wire-version-3 codec. It does not provide partial-text persistence.

## Backend conformance

The shared storage suite exercises checkpoint isolation, status changes,
reconnect cursors, concurrent appends, and lease races and fencing. Its Postgres
parameters skip unless asyncpg and `AGENT_KIT_TEST_PG_DSN` are available:

```bash
AGENT_KIT_TEST_PG_DSN=postgresql://user:password@localhost/linch_test \
  python -m pytest tests/storage/test_run_store_conformance.py \
  tests/storage/test_run_store_leases.py -q
```
