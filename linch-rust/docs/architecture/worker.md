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
| `0005_scheduler.sql` | `linch_worker_agents` (stored agent definitions by digest); interval, time-zone and fixed-session columns on schedules, with `fires`, `skips` and `last_fire`; `agent`, `schedule_id`, `fire_at` and `created_at` on jobs; `linch_worker_history` |
| `0006_background_principal.sql` | `principal` (a `Principal` as JSON) on schedules and jobs; partial indexes on their `agent` column |
| `0007_history_agent.sql` | `agent` on `linch_worker_history`, with partial indexes on `(namespace, run_id)` and `(namespace, agent)` |

A job starts a run (`session_id` and `prompt`), continues one (`run_id`), or
approves one (`run_id` and `approvals`); check constraints keep those shapes
(`migrations/0001_worker.sql:13-31`). A done job moves to
`linch_worker_history` ([job status](#job-status-and-history)); a failed one
keeps its row and error. `fire_due_schedules` reads `linch_runs` (a
heartbeat's live run), so run `linch_postgres::migrate` on the same schema.

## Enqueueing

| Call | Job | Notes |
|---|---|---|
| `enqueue_run(pool, queue, namespace, session_id, prompt)` | start | The worker records the run id on the job before the run's first write, so a retry after a crash continues that run (`crates/linch-worker/src/job.rs:129-163`) |
| `enqueue_resume(pool, queue, namespace, run_id)` | continue | When the run already has an active job, that job's id comes back instead of a new one. The job names the stored definition the run's latest job with one ran with (from `linch_worker_jobs` or `linch_worker_history`), so the worker that served the run serves it again; `WorkerError::Invalid` when that definition was pruned. It names no principal: core continues the run for its start principal (`job.rs`, `enqueue_continuation`) |
| `enqueue_approve(pool, queue, namespace, run_id, approvals)` | approve | Approvals are stored in the core's `ApprovalResponse` wire form. The same definition and principal as `enqueue_resume`. `WorkerError::RunBusy` when the run has an active job, which may end without reading them; enqueue again once it has ended (`job.rs`) |
| `enqueue(pool, &RunRequest, cap)` | start | `enqueue_run` with an optional stored agent definition and an optional [cap](#caps) on the root's unfinished jobs (`crates/linch-worker/src/job.rs`) |

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

It also prunes the history of ended jobs older than `history_retention` (7
days by default) at most once every 10 minutes.

`Worker::definitions(roots, factory)` serves jobs that carry a stored agent
definition: a job with one is taken only by a worker whose `roots` hold the
namespace's first `/`-separated segment, and one without only by a worker
with an agent for its namespace (`crates/linch-worker/src/lease.rs`,
`dequeue`). See [agent definitions](#agent-definitions).

On `shutdown` it waits for the jobs in flight. Dropping the future instead
abandons them like a crash: their runs stay resumable and the jobs become
visible again after the visibility timeout. Database errors inside the loop
are logged and retried on the next poll.

### Concurrency limits

`set_queue_limit(pool, queue, limit)` (`crates/linch-worker/src/lease.rs:107`)
stores a queue's limit with the queue, so every worker enforces the same one;
0 means none. A dequeue on a limited queue takes a transaction-scoped advisory
lock for that queue, counts the jobs held by live workers anywhere (owned,
still hidden, not failed), and takes nothing at the limit
(`lease.rs:134-157`). Jobs held when the limit is lowered keep running.

## Leases and fencing

Two fences protect a run, one per layer:

- **The job.** A dequeue (`lease.rs:125`) takes the oldest visible job of the
  queue in the worker's namespaces with `FOR UPDATE SKIP LOCKED`, hides it for
  the visibility timeout, sets `owner` and adds 1 to `attempts`. That
  `attempts` value is the fencing token: heartbeat, record-run, ack, release
  and fail all match on it, so a worker that lost the job changes nothing
  (`lease.rs:1-6`). Each is one autocommit statement at the pool's default
  isolation. Above `READ COMMITTED`, one that waited on another update of
  the job row (the attempt's own heartbeat still in flight when the run
  ends, or another worker's dequeue) fails with a serialization error
  (`40001`) once that update commits; it wrote nothing and runs again, up to
  three tries in all (`lease.rs:57-87`).
- **The run.** Driving the run claims it in the run store, which adds 1 to its
  epoch and fences out every earlier holder
  ([stores](./stores.md#fencing)). `PgRunStore` also leases the run to the
  worker's agent ([leases](./stores.md#leases)): while another process
  drives the run and renews its lease, a claim is refused with `RunLeased`,
  and the job waits (`Busy`). Keep the run lease no longer than the
  visibility timeout (both 30 s by default), so a job taken over after its
  worker went quiet finds the run free.

While the run streams, `keep_alive` extends the job's visibility every third
of the timeout (`crates/linch-worker/src/execute.rs:151-172`). The job is lost
when a heartbeat finds another holder, or when no extension lands within a
whole timeout of the last confirmed one. Losing the job drops the run stream,
which is a crash for the run: it stays resumable, its run lease is released,
and the next worker's claim fences this one out by epoch. The worker never cancels the run's token for
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

There is no answer job: a run that suspends for an `AskUser` answer ends its
job, a resume job on it only suspends it again, and the answer comes from a
host through `Agent::answer` (or `run.answer`).

## Outcomes, back-off and dead letters

Each attempt ends in one `Outcome` (`crates/linch-worker/src/outcome.rs:9-32`),
which `settle` (`settle.rs:32`) applies:

| Outcome | From | Job |
|---|---|---|
| `Done` | Stream ended at `Result` or `Suspended`; run terminal | Moved to the history as `done` |
| `Retry` | A store or journal error, including a session append conflict; no agent for the namespace | Released with back-off, counting a failure |
| `Busy` | A start job whose session has a live run (`SessionBusy`), or a run another process holds a live lease on (`RunLeased`) | Released with back-off, not counting a failure |
| `Fail` | A late approve (`NotSuspended`), a blocked prompt, undecodable approvals, a missing run | Failed for good |
| `FailRun` | The run's state does not reconcile, or its contract differs from the agent's | The run is failed through `Agent::fail`, then the job |
| `Fenced` | `LeaseLost`: another owner claimed the run | Moved to the history as `superseded`, without a retry, which would take the run back |
| `Lost` | Another worker holds the job | Left alone |

`classify` (`outcome.rs:44`) maps a `RunError` to these. The delay after
attempt `n` is `retry_delay * 2^(n-1)`, capped at 5 minutes
(`settle.rs:24-29`). After five failed attempts the job is failed for good,
and so is its run when the job names one (`settle.rs:77-102`). Waiting for a
busy session never counts, so a start job is never given up on for that.

A failed job is the dead letter: `failed_at` and `error` are set, it is never
dequeued again, and it no longer counts against the one-job-per-run index or
a queue limit (`lease.rs:288-302`). `error` holds the whole error chain
(`crates/linch-worker/src/error.rs:40-49`). When a run is failed with its job,
the run goes first: a crash in between leaves a terminal run, which the job's
next attempt acks (`settle.rs:46-56`).

A factory that refuses a job's definition (`FactoryError::Refused`) fails
it; one that is `Busy` (its tenant has no run place free) releases it like
`Busy` above; one that is `Unavailable` retries it like `Retry`
(`crates/linch-worker/src/execute.rs`, `agent`).

## Job status and history

A job that ends leaves `linch_worker_jobs` and enters `linch_worker_history`
in one statement, a `DELETE ... RETURNING` feeding an `INSERT`, fenced by its
attempt like every other write (`lease.rs`, `ack`), so a job is in exactly
one of the two tables. `job(pool, namespace, id)` and `jobs(pool, namespace,
schedule_id, limit)` (`crates/linch-worker/src/status.rs`) read both, scoped
by the exact namespace (job ids are global), and name a `JobState`: `queued`,
`running` (owned and still hidden), `retrying` (released with an error),
`failed`, `done` or `superseded`. `prune_history(pool, older_than)` deletes
history rows; failed jobs are never pruned.

## Schedules

A `Schedule` (`crates/linch-worker/src/schedule.rs`) fires on a `Timing`
(`crates/linch-worker/src/timing.rs`): a cron pattern (five fields, or six
with leading seconds, croner syntax) read in an IANA time zone through
`chrono-tz`, or a fixed interval. A cron fire in a daylight-saving gap moves
to the first valid time after it; one in an overlap fires once, in the first
half (croner's rules, tested in `timing.rs`). `Timing::shortest_gap` is the
smallest gap over the next 512 fires, which the RPC layer compares with a
tenant's minimum interval.

`create_schedule(pool, &Schedule, cap)` stores it with its first fire, the
first tick after the database's current time; `ScheduleExists` for a taken
id. `list_schedules`, `get_schedule`, `pause_schedule`, `resume_schedule`
(next fire from now: fires missed while paused are not made) and
`delete_schedule` (queued and running jobs are left alone) complete the set.

`fire_due_schedules(pool)` locks up to 64 due rows with
`FOR UPDATE SKIP LOCKED` and, for each, enqueues one start job and moves
`next_fire` to the first tick after **this fire** (not after now), all in one
transaction:

- **Exactly once.** A worker that crashes before the commit leaves neither
  the job nor the move, so the next poll makes the fire; pollers at once skip
  the locked rows. The job's dedupe key `schedule:{id}:{fire time}` is a
  second guard. `tests/pg/scheduler.rs` aborts a fire inside its insert,
  kills the firing backend with `pg_terminate_backend` mid-transaction, and
  races eight firers over twenty schedules.
- **Catch-up.** Since the next fire follows the fire just made, a schedule
  that missed several ticks fires each, in order, one per call, as Python
  Linch's schedules do.
- **Sessions.** Without a `session_id`, each fire starts the session
  `schedule:{id}:{fire time}`. With one, the fire is skipped (`skips` + 1)
  while a job of the schedule is not failed and not ended, or the session
  has a run that is not terminal in `linch_runs` (one suspended for approval
  or input included). A heartbeat's catch-up therefore collapses into one
  run, and fires never pile up behind a run that waits for a person.
- A stored timing that no longer parses pauses its schedule (logged)
  instead of blocking the others.

### Caps

`Cap { root, max }` (`crates/linch-worker/src/definition.rs`) bounds what
the namespaces under one root (a namespace's first `/`-separated segment, a
tenant's name) may hold: schedules for `create_schedule`, unfinished jobs
for `enqueue`. The count runs in the insert's transaction after a
transaction-scoped advisory lock on the root, so writers on any server count
each other's rows; past it, `LimitReached`.

## Agent definitions

A job or schedule may carry an agent definition: text the queue stores once
per `(namespace, SHA-256 digest)` in `linch_worker_agents` and names by
digest, so a schedule's fires share one row and a definition never changes
under a schedule. A worker given `Worker::definitions(roots, factory)` calls
the `AgentFactory` (`crates/linch-worker/src/factory.rs`) once per attempt;
the `JobAgent` it returns is closed after the job is settled, as Python's
worker closes the agent its factory returns. `linch-rpc`'s
`BackgroundAgents` is the factory `linch-server` uses: the definition is an
agent config, re-admitted against its tenant at every attempt
([RPC](./rpc.md#background-runs)).

A definition holds the config's secrets in plain text, so
`prune_definitions(pool)` (`crates/linch-worker/src/definition.rs`) deletes
the ones no schedule, no unfinished job and no history row of a run that
is not terminal names, after setting `agent` to NULL on failed jobs (never
run again). A run suspended for an approval or an answer so keeps its
definition for the job that continues it, until its history row is pruned
(`history_retention`); after that, `enqueue_resume` and `enqueue_approve`
refuse it. A worker runs it with the history
pruning, from its first poll and every 10 minutes after, whether or not it
keeps history. A writer storing a definition locks its row
(`ON CONFLICT ... DO UPDATE`) until it commits, and the prune locks its
candidates with `FOR UPDATE SKIP LOCKED`, so a definition about to be named
is skipped; one named by a writer that committed after the prune read the
references fails the delete's foreign key, and that prune deletes nothing
(it runs again later).

## Principals

A schedule and a job may carry a principal (`Schedule::principal`,
`RunRequest::principal`), stored as JSON; a fire copies its schedule's to
its job. The worker decodes it at every attempt and sets it on the
`RunOptions` of the start, resume or approve (`execute.rs`). Core stores
the start principal with the run (`RunRecord::principal`), and a resume or
approve whose options name none acts for it, so a continuation job
(`enqueue_resume`, `enqueue_approve`) carries none of its own. A row whose
principal does not decode fails its job. Python's worker restores
`RunRequest.principal` the same way (`linch/worker/runtime.py`).

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
    Schedule, Timing, Worker, WorkerConfig, create_schedule, enqueue_run, set_queue_limit,
};
use tokio_util::sync::CancellationToken;

/// Migrate, queue work, and serve it until `shutdown` is cancelled.
async fn serve(
    dsn: &str,
    provider: Arc<dyn Provider>,
    shutdown: CancellationToken,
) -> Result<(), Box<dyn Error>> {
    let (timeout, max) = (linch_postgres::CONNECT_TIMEOUT, linch_postgres::default_max_connections());
    let pool = linch_postgres::connect(dsn, timeout, max).await?;
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
        // A heartbeat: every fire is a new run in this one session.
        session_id: Some(SessionId::new("digest")),
        prompt: "Write the daily digest.".into(),
        timing: Timing::Cron {
            pattern: "0 7 * * *".into(),
            timezone: "Europe/Berlin".into(),
        },
        // No stored definition: the worker's agent for the namespace runs it.
        agent: None,
        // Fires act for no principal: no role-gated rule applies.
        principal: None,
    };
    let stored = create_schedule(&pool, &digest, None).await?;
    println!("first digest at {}", stored.next_fire);

    let mut config = WorkerConfig::new("worker-1");
    config.max_jobs = 4;
    Worker::new(pool, config).agent(agent).run(shutdown).await?;
    Ok(())
}

fn main() {
    let _ = serve; // needs a database
}
```

The example's pool holds `default_max_connections()`, the machine's cores between 10 and 32. A worker with `max_jobs`
runs committing at once wants about two connections per run plus the
listener; see [connecting and sizing a pool](./stores.md#connecting-and-sizing-a-pool).

## Tests

`crates/linch-worker/tests/pg/` covers one job per run, exclusive holds and
queue limits, retries and back-off, busy sessions, suspend and approve,
takeover after a lost heartbeat (the test agents lease runs for 1.5 s,
under the 3 s visibility timeout of a worker a test means to lose),
schedule firing (catch-up, heartbeat skips, pause and resume, caps,
a fire aborted or killed mid-transaction, racing firers), jobs run by an
`AgentFactory` and their history, and the `READ COMMITTED`
pin; `crates/linch-worker/src/lease/tests.rs` runs each fenced write behind
another update of its job row under `REPEATABLE READ`. Each test skips when
`LINCH_TEST_PG_DSN` is unset.

---

Back to the [architecture index](./README.md).
