# Stores

> Part of the [linch-rust architecture guide](./README.md).

Durable state lives behind four traits: `RunStore`, `SessionStore`,
`TaskStore` (all in `crates/linch-core/src/store.rs` and `store/tasks.rs`) and
`FileBackend` (`crates/linch-core/src/vfs.rs`). Each has an in-memory
implementation for tests and development, a Postgres one in `linch-postgres`,
and a conformance suite every implementation must pass. Use this page when
you write a store, change a write path or a migration, or size a pool.

Python Linch describes the same model in
[durable runs, leases and checkpoints](../../../docs/architecture/subsystems.md#33-durable-runs-leases-and-checkpoints).
What differs in Rust: one `commit` call is the only write to a running run,
Postgres is the only durable backend, and the Postgres writes on the hot path
are single statements.

## Keys and tenancy

Every record is keyed by a host-chosen namespace plus an id, so tenants never
share a key space (`store.rs:3-4`). An agent's namespace
(`AgentBuilder::namespace`) is the namespace of its runs and sessions;
`task_tools` takes the namespace of the sessions it serves.

## Run store

`RunStore` (`crates/linch-core/src/store.rs:163`):

| Method | Contract |
|---|---|
| `create(NewRun)` | One write: the run row at status `pending`, epoch 0, with its first checkpoint and events (seqs from 1). `Conflict` when the id exists; `SessionBusy` when the namespace already holds a non-terminal run of that session. The check and the insert are one atomic step (`store.rs:164-171`) |
| `claim(namespace, run_id, owner)` | Take ownership: epoch + 1, status `running`, a `Lease`. The only way to `running`, including out of `awaiting_*`. Refused on a terminal run |
| `load` | The current `RunRecord` |
| `commit(&Lease, Commit)` | One atomic write of events (seqs continue `last_seq` with no gap), an optional checkpoint and an optional status. Accepted only on a `running` run under the current epoch. A terminal status is final |
| `events(namespace, run_id, after_seq)` | Committed events after `after_seq`, in order |

`StoreError` (`store.rs:104`) names each refusal: `NotFound`, `LeaseLost`,
`Conflict`, `SessionBusy`, `SeqGap`, `InvalidTransition` and `Backend`. A
rejected write changes nothing.

### One live run per session

A session holds at most one run that is not terminal, per namespace. The
store refuses the second `create` before anything of it is written, so two
runs never interleave in one session log (`crates/linch-core/src/runner.rs:30-34`).
`SessionBusy` carries the live run's id: resume, approve or fail it first.
Terminal statuses are `completed`, `failed` and `aborted`
(`crates/linch-core/src/run.rs:56`).

### Fencing

`claim` always wins and adds 1 to the epoch; every later `commit` under an
older lease fails with `LeaseLost`. There is no expiry and no heartbeat in the
store: a worker that holds a run is fenced out by the next claim, never by a
timeout. The worker adds visibility timeouts on top
([worker](./worker.md#leases-and-fencing)).

## Session store

`SessionStore` (`store.rs:199`) holds the append-only session log:

| Method | Contract |
|---|---|
| `create(namespace, session_id)` | An empty log; `Conflict` if it exists |
| `load` | Every entry, in order |
| `append(namespace, session_id, expected_len, entries)` | Append only if the log has exactly `expected_len` entries, else `Conflict`; returns the new length |

`expected_len` is the optimistic lock: of two appends at the same length,
one lands and the other gets `Conflict`.

## Task store

`TaskStore` (`crates/linch-core/src/store/tasks.rs:186`) keeps each session's
tasks, a small dependency graph behind the task tools. The session stores
implement it. Ids are decimal numbers handed out per session from 1 and never
reused. An edge "`a` blocks `b`" may name a task that does not exist yet; it
shows up once that task is created, and deleting a task removes every edge
that names it. `claim_task` has exactly one winner among concurrent claims
(`tasks.rs:236`); `ready_tasks` lists pending, unowned tasks whose blockers
all exist and are completed (`tasks.rs:255`). Tasks are session data: only
the tool calls and their results enter the session log.

## Conformance suites

| Suite | For | Where |
|---|---|---|
| `run_store_suite` | `RunStore` | `crates/linch-core/src/store/conformance.rs:33` |
| `run_admission_suite` | `RunStore` (one live run per session) | `store/conformance/admission.rs:12` |
| `session_store_suite` | `SessionStore` | `store/conformance.rs:42` |
| `task_store_suite` | `SessionStore + TaskStore` | `store/conformance/tasks.rs:14` |
| `backend_suite` | `FileBackend` | `crates/linch-core/src/vfs/conformance.rs:11` |

A failed rule panics with a message naming it, so call the suites from a
test. Each check uses its own namespace under `conformance/`; run them on a
store that holds nothing there. The run lifecycle they pin down
(`store/conformance.rs:10-16`): `create` stores a `pending` run at epoch 0;
`claim` is the only way to `running` and adds 1 to the epoch; `commit` is
accepted only on a `running` run under the current epoch; a terminal status
is final, and a rejected commit changes nothing.

## Memory stores

`MemoryRunStore` and `MemorySessionStore`
(`crates/linch-core/src/store/memory.rs:45`, `memory.rs:227`) follow the same
lifecycle and pass the same suites. They count every write that lands, per
method (`writes()`, `memory.rs:68`, `memory.rs:246`), so a test or bench can
assert store writes per turn; rejected writes and task writes are not
counted. `store::faulty::Faulty` (`crates/linch-core/src/store/faulty.rs:43`)
wraps either and fails write number `n`, before or after it lands, for crash
tests.

## Postgres stores

`linch-postgres` implements `PgRunStore`, `PgSessionStore` (also the
`TaskStore`) and `PgFileBackend`. All take a `PgPool`.

### Schema and migrations

`migrate(&pool)` (`crates/linch-postgres/src/lib.rs:227`) creates or upgrades
the tables. It is safe on every start: applied migrations are skipped and
concurrent callers serialize on an advisory lock. Applied versions are
recorded in `linch_migrations` (`lib.rs:62`), not sqlx's default table, so a
host's own sqlx migrations in the same schema do not collide. Tables are
unqualified and prefixed `linch_`, so the connection's `search_path` picks the
schema. Queries are built at run time: building the crate needs no database.

| Migration | Adds |
|---|---|
| `0001_stores.sql` | `linch_runs`, `linch_run_events`, `linch_sessions`, `linch_session_entries` |
| `0002_one_live_run_per_session.sql` | The partial unique index `linch_runs_one_live_per_session` on `(namespace, session_id)` where the status is not terminal |
| `0003_files.sql` | `linch_files`, with `path` in `COLLATE "C"` so the primary key orders by bytes and serves prefix scans |
| `0004_tasks.sql` | `linch_sessions.next_task_id`, `linch_tasks`, `linch_task_edges` |
| `0005_mailbox.sql` | `linch_mailbox` (`PgMailbox`: peer messages by namespace and recipient, leased by a drain, deleted by its ack) |

JSON payloads (checkpoints, envelopes, log entries, task metadata) are `TEXT`,
not `JSONB`: JSONB reorders keys and rewrites numbers, and the stores must
hand back exactly what was written, because message bytes feed prompt caches
and digests (`migrations/0001_stores.sql:1-3`). Rows decode strictly; a row
that does not decode is an error (`crates/linch-postgres/src/codec.rs:1-4`).
Migrations are append-only: add a new file and a `Migration::new` entry
(`lib.rs:169-211`), never edit an applied one, since `migrate` fails when an
applied migration differs from the embedded one.

### Write paths

| Write | Shape |
|---|---|
| Run `commit` | One autocommit statement (`crates/linch-postgres/src/run.rs:72-82`): an `UPDATE` that moves the run row only under the lease's epoch, while `running`, and when `last_seq` is the one the events continue; the events are inserted from the moved row, so they land with it or not at all |
| Session `append` | One autocommit statement (`crates/linch-postgres/src/session.rs:17-24`): the length moves only from `expected_len`, and the entries are inserted from the moved row at positions `expected_len + n - 1` |
| Run `create` | A transaction: insert the row with `ON CONFLICT (namespace, run_id) DO NOTHING`, then the first events (`run.rs:182`) |
| Run `claim` | A transaction that locks the row `FOR UPDATE`, then sets status, epoch and owner (`run.rs:259`) |
| Task writes | A transaction that locks the session row first, so a session's task writes serialize: ids come out in order with no gaps and a claim has one winner (`crates/linch-postgres/src/tasks.rs:1-8`) |
| File `edit` | A transaction that locks the file row `FOR UPDATE` (`crates/linch-postgres/src/files.rs:1-6`) |

Multi-statement writes begin at `READ COMMITTED` whatever the server or role
default, because they must read a row again after waiting on another writer
(`lib.rs:148`). The single-statement writes run in autocommit at the pool's
default isolation; above `READ COMMITTED` a serialization failure (`40001`)
wrote nothing and is retried, up to three tries in all (`lib.rs:153-166`).

Admission is enforced by the partial unique index, not by a read. A violation
of `linch_runs_one_live_per_session` reads the live run's id and returns
`SessionBusy`; if that run ended in between, the insert is tried again, up to
three times (`run.rs:128-152`, `run.rs:222-244`).

### Refusal classification

When a commit's statement moves no row, the store reads the row once more and
names the refusal in the same order as the memory store (`run.rs:86-102`,
`codec.rs:120`):

1. no row: `NotFound`;
2. another epoch: `LeaseLost`;
3. status not `running`, or a commit to `pending`: `InvalidTransition`;
4. per event, in order: an event of another run is `Conflict`, a seq that does
   not continue is `SeqGap`.

If the read finds nothing wrong, the row moved after the refused write and the
commit is tried again. After three tries it is `Conflict` ("kept changing
during a commit") (`run.rs:320-354`). A commit that no row could take (a
status of `pending`, an event of another run, or seqs that do not continue
each other) skips the statement and goes straight to the read. A session append reads the length
the same way: `NotFound`, or `Conflict` naming both lengths
(`session.rs:146-168`).

### Connecting and sizing a pool

`linch_postgres::connect(dsn, timeout, max_connections)` (`lib.rs:104`) bounds
the first connection (default `CONNECT_TIMEOUT`, 5 s); sqlx alone would retry a
refused connection for its 30 s acquire timeout. Past it, the error names the
host and port (or socket) it tried. No error message holds the DSN.

The pool pings a connection before handing it out only when it sat idle for
`IDLE_PING_AFTER` (1 s) or longer, and replaces it when the ping fails
(`before_acquire`, `lib.rs:119-128`). sqlx does not watch an idle socket, so
without the ping a connection the server closed while idle (a restart, a
failover, `idle_session_timeout`, a proxy's idle kill) would fail the next
write on it. After a model call, that write is the receipt: the journal would
be poisoned, and the resume would find no accepted receipt and pay for the
call again. Writes that follow each other closely, such as a run's barrier
writes, skip the ping, so a run commit or a session append stays one round
trip; sqlx's own ping before every acquire (`test_before_acquire`, off here)
would make each of them two. A write after a pause of 1 s or more, such as the
receipt after a model call, pays one extra round trip. A connection the server
closes within a second of its last use still fails its next write, and the
run stops at its last durable state. sqlx's idle timeout and lifetime still
recycle connections.

`connect` keeps sqlx's other defaults. Its default size,
`default_max_connections()` (`crates/linch-postgres/src/pool_size.rs:26`), is the
machine's cores clamped to `MIN_DEFAULT_CONNECTIONS` (10, sqlx's own default)
and `MAX_DEFAULT_CONNECTIONS` (32), computed once per process; it replaces
the fixed `MAX_CONNECTIONS` (10), which is gone. The floor keeps
a small machine at about five runs committing at once. The ceiling is for the
database: Postgres's own `max_connections` (100 unless set) is shared by every
pool of every process on it, so three processes at the default still fit. In
the case 14 bench (2000 sessions, Postgres stores) a pool of 10 was the
bottleneck: 3355 ms of overhead per turn, against 382 ms with 40 connections;
with the default of 32 on a 64-core machine it was 646 ms (3914 ms with 10, in
the same back-to-back run). A facade config sets its own size with
`stores.max_connections`; on `linch-server` a tenant may ask for at most its
`max_pool_connections` ([rpc](./rpc.md)). sqlx
also pings a connection in the background when it is released (sqlx-core
0.9.0, `src/pool/connection.rs:314`), so a run that commits back to
back alternates between two connections. Size a pool at about two
connections per concurrently committing run. A `Worker` needs one more for its
`LISTEN` connection (`crates/linch-worker/src/worker.rs:87-90`). The example
below sizes its pool that way with `connect`.

## Group-commit write counts

The run journal (`crates/linch-core/src/journal.rs`) holds events and the
checkpoint in memory and writes them in one `commit` at each barrier: before a
provider dispatch, around each tool that really runs, before each session
write, and at the end. A held checkpoint covers only the events pending when
it was saved, so every committed state is one that writing each step through
would also have produced (`journal.rs:1-31`).

A tool turn with `k` started tools costs `3 + 2k` run writes and 2 session
writes (`crates/linch-core/src/runner.rs:36-37`). The count is pinned by
`a_tool_turn_costs_three_plus_two_k_run_writes_and_two_session_writes`
(`crates/linch-core/src/journal/tests/turn.rs:116`):

| Step | Run writes | Session writes |
|---|---|---|
| `ProviderPending` is held; the delivery event and that checkpoint are flushed before dispatch | 1 | |
| `AssistantReceived` writes through with the receipt, `Assistant` and `Usage` events | 1 | |
| The response is appended to the session (nothing is held, so no barrier write) | | 1 |
| `ToolBatch` is held; each started tool flushes its start before it runs (the first also carries `ToolBatch`) and its end after | 2k | |
| `ToolResults` is held; `flush_checkpoint` writes it before the results are appended | 1 | |
| The results are appended to the session | | 1 |

A call that runs nothing (refused, served by a hook, aborted before it
started) adds no barrier: its start and end ride the next write. A text-only
final turn ends with one terminal write that carries the pending events, the
`Result` and the status together (`journal.rs:233-238`).

## Example

This compiles at this revision; `main` runs every suite on the memory stores,
and the two functions need a database:

```rust
use std::error::Error;
use std::num::NonZeroU32;

use linch_core::store::conformance::{
    run_admission_suite, run_store_suite, session_store_suite, task_store_suite,
};
use linch_core::vfs::conformance::backend_suite;
use linch_core::{MemoryRunStore, MemorySessionStore};
use linch_postgres::{PgFileBackend, PgPool, PgRunStore, PgSessionStore};

/// A pool for `runs` runs committing at once: about two connections each.
async fn pool(dsn: &str, runs: NonZeroU32) -> Result<PgPool, sqlx::Error> {
    let max = runs.saturating_mul(NonZeroU32::new(2).unwrap());
    let pool = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT, max).await?;
    linch_postgres::migrate(&pool).await?;
    Ok(pool)
}

/// Every suite a store backend must pass, here run on Postgres.
async fn check_postgres(dsn: &str) -> Result<(), Box<dyn Error>> {
    let (timeout, max) = (linch_postgres::CONNECT_TIMEOUT, linch_postgres::default_max_connections());
    let pool = linch_postgres::connect(dsn, timeout, max).await?;
    linch_postgres::migrate(&pool).await?;
    let runs = PgRunStore::new(pool.clone());
    run_store_suite(&runs).await;
    run_admission_suite(&runs).await;
    let sessions = PgSessionStore::new(pool.clone());
    session_store_suite(&sessions).await;
    task_store_suite(&sessions).await;
    backend_suite(&PgFileBackend::new(pool, "conformance/files")).await;
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let runs = MemoryRunStore::new();
    run_store_suite(&runs).await;
    run_admission_suite(&runs).await;
    let sessions = MemorySessionStore::new();
    session_store_suite(&sessions).await;
    task_store_suite(&sessions).await;
    backend_suite(&linch_tools::vfs::MemoryBackend::new()).await;
    let _ = (pool, check_postgres); // need a database
}
```

The Postgres tests (`crates/linch-postgres/tests/pg/`) run these suites and
the refusal, admission, crash and isolation cases, each in its own schema.
Each test skips when `LINCH_TEST_PG_DSN` is unset.
Each test opens about six connections, so on a machine with 16 or more
cores run them with `RUST_TEST_THREADS=8` to stay under Postgres's
`max_connections` of 100.

---

Back to the [architecture index](./README.md).
