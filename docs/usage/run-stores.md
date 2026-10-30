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
memory, and run stores.

```python
from linch import Agent, PostgresRunStore, PostgresSessionStore

runs = PostgresRunStore("postgresql://user:password@localhost/linch")
sessions = PostgresSessionStore("postgresql://user:password@localhost/linch")
agent = Agent(model="gpt-5", session_store=sessions, run_store=runs)
```

Close stores with `await store.close()` when their host shuts down.
`PostgresRunStore` accepts `pool=`, `min_size=`, and `max_size=` like the
Postgres session store. A supplied pool follows the store's lifecycle; closing
the store closes that pool.

## Optional leases and fencing

All three built-in stores implement the optional `RunLeaseStore` capability.
The required `RunStore` protocol stays unchanged, so existing custom stores
continue to work. Hosts can detect lease support with `getattr(store, "claim",
None)`.

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
writes check that epoch in the same transaction as the write. If another worker
claims an expired lease, the epoch increases and the previous holder's next
write raises `LeaseLostError`. The failed write changes no persisted state.
Expiry alone permits takeover; writes are fenced by epoch, not by a local
clock check. An expired lease cannot be renewed.

Release clears ownership and advances the epoch when the lease still matches.
Every release attempt also removes this instance's holder-local state for the
run, including when it raises `LeaseLostError` after a takeover. Releasing a
stale lease does not affect the current owner. A fenced write retains its local
lease state until the host calls release, so further zombie writes stay fenced.

Hosts call release in `finally` after stopping all work for the run. Release
ends fencing for that run on this instance; do not write to it after releasing
it. The local ownership maps therefore track active ownership rather than
growing with every run a long-lived worker has ever claimed.

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

Postgres transactions serialize per-run event sequence allocation across store
instances. Asyncpg performs database I/O asynchronously.

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
events; it does not provide partial-text persistence.

## Backend conformance

The shared storage suite exercises checkpoint isolation, status changes,
reconnect cursors, concurrent appends, and lease races and fencing. Its Postgres
parameters skip unless asyncpg and `AGENT_KIT_TEST_PG_DSN` are available:

```bash
AGENT_KIT_TEST_PG_DSN=postgresql://user:password@localhost/linch_test \
  python -m pytest tests/storage/test_run_store_conformance.py \
  tests/storage/test_run_store_leases.py -q
```
