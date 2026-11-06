# Worker

> Part of the [linch-rust architecture guide](./README.md).

`linch-worker` (`crates/linch-worker/src/`) is the Postgres job queue, the
worker fleet that drains it, and cron schedules that feed it. A host enqueues
work; any number of `Worker`s, in any number of processes, take jobs and drive
runs through the public `Agent` API (`run`, `resume`, `approve`). Use this
page when you change how jobs are taken, held, retried or failed.

Python Linch runs its fleet on PgQueuer
([worker dispatch](../../../docs/architecture/subsystems.md#34-worker-dispatch-and-pgqueuer-recovery)).
The Rust queue is a plain table dequeued with `FOR UPDATE SKIP LOCKED` and
woken by `LISTEN/NOTIFY`. It keeps PgQueuer's defaults for the failure cap
and the longest retry delay (`crates/linch-worker/src/settle.rs:14-20`).

## Tables

`linch_worker::migrate(&pool)` (`crates/linch-worker/src/lib.rs:135`) creates
the tables, recording versions in `linch_worker_migrations` so they never
collide with `linch_postgres`'s. Run both crates' `migrate` at startup. Every
time is the database clock (`now()`), never a worker's.

| Migration | Adds |
|---|---|
| `0001_worker.sql` | `linch_worker_jobs` (one row per job) and `linch_worker_schedules` |
| `0002_one_job_per_run.sql` | The partial unique index `linch_worker_jobs_one_per_run` on `(namespace, run_id)` where `failed_at IS NULL` |
| `0003_job_failures.sql` | `failures`, counted apart from `attempts` |
| `0004_queue_limits.sql` | `linch_worker_queues` (`name`, `max_held`) |

A job starts a run (`session_id` and `prompt`), continues one (`run_id`), or
approves one (`run_id` and `approvals`); check constraints keep those shapes
(`migrations/0001_worker.sql:13-31`). A done job is deleted; a failed one keeps
its row and error.

## Enqueueing

| Call | Job | Notes |
|---|---|---|
| `enqueue_run(pool, queue, namespace, session_id, prompt)` | start | The worker records the run id on the job before the run's first write, so a retry after a crash continues that run (`crates/linch-worker/src/job.rs:129-163`) |
| `enqueue_resume(pool, queue, namespace, run_id)` | continue | When the run already has an active job, that job's id comes back instead of a new one (`job.rs:164-196`) |
| `enqueue_approve(pool, queue, namespace, run_id, approvals)` | approve | Approvals are stored in the core's `ApprovalResponse` wire form. `WorkerError::RunBusy` when the run has an active job, which may end without reading them; enqueue again once it has ended (`job.rs:197-239`) |

Each insert and its `pg_notify` on channel `linch_worker` (`lib.rs:76`) run in
one transaction, so the wake-up is delivered when the job is committed
(`job.rs:49-109`). A job that names a run gives way to the run's active job;
a start job gives way to an earlier job with the same dedupe key.

## One active job per run

Two jobs on one run would claim it from each other. The unique index allows
at most one job per run that has not failed. A start job names its run only
once it records it, so it never conflicts before
(`migrations/0002_one_job_per_run.sql`).

## The worker loop

`WorkerConfig::new(owner)` (`crates/linch-worker/src/worker.rs:53`):

| Field | Default | Meaning |
|---|---|---|
| `owner` | required | Written with every run claim and job hold; unique per worker |
| `queues` | `["default"]` | Served one job from each in turn |
| `max_jobs` | 32 | Most jobs this worker runs at once |
| `visibility_timeout` | 30 s | How long a dequeued job stays hidden without a heartbeat |
| `poll_interval` | 1 s | Longest wait between polls without a notification |
| `retry_delay` | 5 s | Wait before a job's second attempt; doubles each attempt |

`Worker::new(pool, config).agent(agent)` serves the jobs of
`agent.namespace()`; add one agent per namespace. Every worker serving a
namespace needs an agent of the same contract, and every agent must store its
runs through `PgRunStore` in the worker pool's database and schema, because the
worker reads run records there (`worker.rs:84-90`). The pool needs room for
one `LISTEN` connection plus the job queries.

`Worker::run(shutdown)` (`worker.rs:128`):

1. checks the config and opens the `LISTEN` connection; a failed receive
   falls back to polling and retries after 1 s (`worker.rs:224-234`);
2. fires due schedules, at most once per `poll_interval` however often
   notifications wake it (`worker.rs:146-152`);
3. takes one job from each queue in turn until it holds `max_jobs` or every
   queue is empty, and runs each job in its own task (`worker.rs:173-212`);
4. waits for a notification, a finished job, the poll interval or `shutdown`.

On `shutdown` it waits for the jobs in flight. Dropping the future instead
abandons them like a crash: their runs stay resumable and the jobs become
visible again after the visibility timeout. Database errors inside the loop
are logged and retried on the next poll.

### Concurrency limits

`set_queue_limit(pool, queue, limit)` (`crates/linch-worker/src/lease.rs:72`)
stores a queue's limit with the queue, so every worker enforces the same one;
0 means none. A dequeue on a limited queue takes a transaction-scoped advisory
lock for that queue, counts the jobs held by live workers anywhere (owned,
still hidden, not failed), and takes nothing at the limit
(`lease.rs:99-122`). Jobs held when the limit is lowered keep running.

## Leases and fencing

Two fences protect a run, one per layer:

- **The job.** A dequeue (`lease.rs:90`) takes the oldest visible job of the
  queue in the worker's namespaces with `FOR UPDATE SKIP LOCKED`, hides it for
  the visibility timeout, sets `owner` and adds 1 to `attempts`. That
  `attempts` value is the fencing token: heartbeat, record-run, ack, release
  and fail all match on it, so a worker that lost the job changes nothing
  (`lease.rs:1-4`).
- **The run.** Driving the run claims it in the run store, which adds 1 to its
  epoch and fences out every earlier holder
  ([stores](./stores.md#fencing)).

While the run streams, `keep_alive` extends the job's visibility every third
of the timeout (`crates/linch-worker/src/execute.rs:151-172`). The job is lost
when a heartbeat finds another holder, or when no extension lands within a
whole timeout of the last confirmed one. Losing the job drops the run stream,
which is a crash for the run: it stays resumable, and the next worker's claim
fences this one out by epoch. The worker never cancels the run's token for
this, because a cancel ends the run `aborted` (`execute.rs:5-9`).

### Choosing run, resume or approve

`open` (`execute.rs:74-104`) decides from the job and the run's durable
record:

| Job and run | Action |
|---|---|
| Start job, no run yet (or its create never landed) | `Agent::run`; the run id is recorded on the job first |
| Run is terminal | Nothing; the job is done |
| Approve job, run suspended for approval, or a first attempt | `Agent::approve`; on a run that is not suspended the core refuses it (`NotSuspended`) and the job fails |
| Approve job, later attempt, run no longer suspended | `Agent::resume`: an earlier attempt committed the answers (logged as a warning) |
| Resume job, run suspended for approval | Nothing; the run waits for an approve job |
| Resume job, run not suspended | `Agent::resume` |

"Suspended" is the checkpoint phase, not the status: an approve that crashed
between its claim and its commit leaves the status `running`
(`execute.rs:116-118`). A run that suspends for approval ends its job;
`enqueue_approve` queues the continuation.

## Outcomes, back-off and dead letters

Each attempt ends in one `Outcome` (`crates/linch-worker/src/outcome.rs:9-32`),
which `settle` (`settle.rs:32`) applies:

| Outcome | From | Job |
|---|---|---|
| `Done` | Stream ended at `Result` or `Suspended`; run terminal | Deleted |
| `Retry` | A store or journal error, including a session append conflict; no agent for the namespace | Released with back-off, counting a failure |
| `Busy` | A start job whose session has a live run (`SessionBusy`) | Released with back-off, not counting a failure |
| `Fail` | A late approve (`NotSuspended`), a blocked prompt, undecodable approvals, a missing run | Failed for good |
| `FailRun` | The run's state does not reconcile, or its contract differs from the agent's | The run is failed through `Agent::fail`, then the job |
| `Fenced` | `LeaseLost`: another owner claimed the run | Deleted without a retry, which would take the run back |
| `Lost` | Another worker holds the job | Left alone |

`classify` (`outcome.rs:44`) maps a `RunError` to these. The delay after
attempt `n` is `retry_delay * 2^(n-1)`, capped at 5 minutes
(`settle.rs:24-29`). After five failed attempts the job is failed for good,
and so is its run when the job names one (`settle.rs:77-102`). Waiting for a
busy session never counts, so a start job is never given up on for that.

A failed job is the dead letter: `failed_at` and `error` are set, it is never
dequeued again, and it no longer counts against the one-job-per-run index or
a queue limit (`lease.rs:247-258`). `error` holds the whole error chain
(`crates/linch-worker/src/error.rs:40-49`). When a run is failed with its job,
the run goes first: a crash in between leaves a terminal run, which the job's
next attempt acks (`settle.rs:46-56`).

## Schedules

`create_schedule(pool, &Schedule)` (`crates/linch-worker/src/schedule.rs:80`)
stores a cron pattern in UTC (five fields, or six with leading seconds, croner
syntax) and returns its first fire, the first tick after the database's
current time. `cancel_schedule` disables future fires; runs already enqueued
stay queued.

`fire_due_schedules(pool)` (`schedule.rs:165`) locks up to 64 due rows with
`FOR UPDATE SKIP LOCKED`, enqueues one start job per row and moves `next_fire`
forward, all in one transaction. A fire therefore happens once however many
workers poll. Each job also carries the dedupe key `schedule:{id}:{fire time}`
as a second guard (`schedule.rs:1-12`). Missed fires are not replayed: a
schedule overdue by several ticks fires once, and its next fire is the first
tick after now. A stored pattern that no longer parses disables its schedule
instead of blocking the others.

## Example

This compiles at this revision; it needs a database to run:

```rust
use std::error::Error;
use std::sync::Arc;

use linch_core::{
    AgentBuilder, AgentSpec, PermissionEngine, PermissionMode, PermissionPolicy, Provider,
    SessionId,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use linch_worker::{
    Schedule, Worker, WorkerConfig, create_schedule, enqueue_run, set_queue_limit,
};
use tokio_util::sync::CancellationToken;

/// Migrate, queue work, and serve it until `shutdown` is cancelled.
async fn serve(
    dsn: &str,
    provider: Arc<dyn Provider>,
    shutdown: CancellationToken,
) -> Result<(), Box<dyn Error>> {
    let pool = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT).await?;
    linch_postgres::migrate(&pool).await?;
    linch_worker::migrate(&pool).await?;

    // The worker reads run records through PgRunStore on its own pool, so
    // the agent's runs must live in the same database and schema.
    let permissions = PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/"))?;
    let agent = AgentBuilder::new(AgentSpec::new("my-model"))
        .provider(provider)
        .permissions(permissions)
        .run_store(Arc::new(PgRunStore::new(pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace("tenant-a")
        .build()?;

    set_queue_limit(&pool, "default", 8).await?;
    let session = SessionId::new("inbox");
    enqueue_run(&pool, "default", "tenant-a", &session, "Summarize the inbox.").await?;
    let digest = Schedule {
        namespace: "tenant-a".into(),
        id: "daily-digest".into(),
        queue: "default".into(),
        session_id: SessionId::new("digest"),
        prompt: "Write the daily digest.".into(),
        cron: "0 7 * * *".into(),
    };
    let first_fire = create_schedule(&pool, &digest).await?;
    println!("first digest at {first_fire}");

    let mut config = WorkerConfig::new("worker-1");
    config.max_jobs = 4;
    Worker::new(pool, config).agent(agent).run(shutdown).await?;
    Ok(())
}

fn main() {
    let _ = serve; // needs a database
}
```

`connect`'s pool holds at most 10 connections. A worker with `max_jobs`
runs committing at once wants about two connections per run plus the
listener; see [connecting and sizing a pool](./stores.md#connecting-and-sizing-a-pool).

## Tests

`crates/linch-worker/tests/pg/` covers one job per run, exclusive holds and
queue limits, retries and back-off, busy sessions, suspend and approve,
takeover after a lost heartbeat, schedule firing, and the `READ COMMITTED`
pin. Each test skips when `LINCH_TEST_PG_DSN` is unset.

---

Back to the [architecture index](./README.md).
