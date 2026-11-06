//! Postgres job queue, worker fleet and cron schedules for Linch runs.
//!
//! A host enqueues work with [`enqueue_run`], [`enqueue_resume`] or
//! [`enqueue_approve`]; any number of [`Worker`]s, in any number of
//! processes, take jobs off the queue and drive the runs through the public
//! [`linch_core::Agent`] API. [`create_schedule`] adds a cron schedule whose
//! fires enqueue new runs.
//!
//! - **Queue.** A plain table, dequeued with `FOR UPDATE SKIP LOCKED` and
//!   woken by `LISTEN/NOTIFY` (with polling as the fallback). A dequeued job
//!   is hidden for a visibility timeout that its worker's heartbeat extends,
//!   so a crashed worker's job becomes visible again and another worker
//!   takes it over. The run's lease epoch fences the old worker out. A
//!   queue's concurrency limit ([`set_queue_limit`]) is stored with it and
//!   counted across the fleet.
//! - **Runs.** A worker starts a run with [`linch_core::Agent::run`] and
//!   continues it with `resume` or `approve`. A run has at most one active
//!   job, and a job whose run another owner claimed is dropped, so two
//!   holders never take a run from each other. A run that suspends for
//!   approval ends its job; [`enqueue_approve`] queues the continuation. A
//!   failed attempt is retried after a delay that doubles each time. A job
//!   that can never succeed, or whose attempts failed 5 times, is kept as
//!   failed; when its run can never continue either (its durable state does
//!   not reconcile, it has another contract, or its job gave up),
//!   [`linch_core::Agent::fail`] marks the run failed first. A start job
//!   whose session has a live run waits for it, backing off the same way,
//!   and is never given up on.
//! - **Schedules.** A due schedule enqueues one start job and moves its next
//!   fire forward in one transaction, so a fire happens once however many
//!   workers poll.
//!
//! The worker reads run records through [`linch_postgres::PgRunStore`] on
//! its own pool, so every agent it drives must store its runs in the same
//! database and schema (`search_path`). Run [`linch_postgres::migrate`] and
//! [`migrate`] once at startup:
//!
//! ```no_run
//! # async fn start(dsn: &str) -> Result<(), Box<dyn std::error::Error>> {
//! use linch_postgres::{CONNECT_TIMEOUT, MAX_CONNECTIONS};
//!
//! let pool = linch_postgres::connect(dsn, CONNECT_TIMEOUT, MAX_CONNECTIONS).await?;
//! linch_postgres::migrate(&pool).await?;
//! linch_worker::migrate(&pool).await?;
//! # Ok(()) }
//! ```

#![forbid(unsafe_code)]

use std::borrow::Cow;

use linch_postgres::{MigrateError, PgPool};
use sqlx::migrate::{Migration, MigrationType, Migrator};
use sqlx::{Postgres, SqlSafeStr, Transaction};

mod error;
mod execute;
mod job;
mod lease;
mod outcome;
mod schedule;
mod settle;
mod worker;

pub use error::WorkerError;
pub use job::{JobId, enqueue_approve, enqueue_resume, enqueue_run};
pub use lease::set_queue_limit;
pub use schedule::{Schedule, cancel_schedule, create_schedule, fire_due_schedules};
pub use worker::{Worker, WorkerConfig};

/// Table that records which worker migrations have been applied.
///
/// It is neither sqlx's `_sqlx_migrations` nor linch-postgres's
/// `linch_migrations`, so the two crates' migration versions never collide.
pub const MIGRATIONS_TABLE: &str = "linch_worker_migrations";

/// The `LISTEN/NOTIFY` channel that every enqueue notifies; the payload is
/// the queue name.
pub const CHANNEL: &str = "linch_worker";

/// Begin a READ COMMITTED transaction whatever the server or role default.
///
/// The queue's row locks, and the count taken after a limited queue's lock,
/// must see what other workers committed while this one waited; REPEATABLE
/// READ would keep the snapshot from before the wait.
pub(crate) async fn begin(pool: &PgPool) -> Result<Transaction<'static, Postgres>, sqlx::Error> {
    pool.begin_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        .await
}

fn migrator() -> Migrator {
    let simple = |version, name, sql: &'static str| {
        let sql = sql.into_sql_str();
        Migration::new(
            version,
            Cow::Borrowed(name),
            MigrationType::Simple,
            sql,
            false,
        )
    };
    let migrations = vec![
        simple(1, "worker", include_str!("../migrations/0001_worker.sql")),
        simple(
            2,
            "one job per run",
            include_str!("../migrations/0002_one_job_per_run.sql"),
        ),
        simple(
            3,
            "job failures",
            include_str!("../migrations/0003_job_failures.sql"),
        ),
        simple(
            4,
            "queue limits",
            include_str!("../migrations/0004_queue_limits.sql"),
        ),
    ];
    Migrator {
        table_name: Cow::Borrowed(MIGRATIONS_TABLE),
        ..Migrator::with_migrations(migrations)
    }
}

/// Create or upgrade the queue and schedule tables in the pool's database.
///
/// Safe to call on every start: applied migrations are skipped, and
/// concurrent callers serialize on a Postgres advisory lock. The run and
/// session tables come from [`linch_postgres::migrate`].
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to migrate.
///
/// Raises:
///     MigrateError: If the database is unreachable, a migration fails, or
///         an applied migration differs from the embedded one.
pub async fn migrate(pool: &PgPool) -> Result<(), MigrateError> {
    migrator().run(pool).await
}
