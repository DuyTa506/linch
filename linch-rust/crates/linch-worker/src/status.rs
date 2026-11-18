//! What became of a job: its state while queued or held, and its history
//! row once it ended.

use std::time::Duration;

use chrono::{DateTime, Utc};
use linch_core::{RunId, SessionId};
use linch_postgres::PgPool;

use crate::error::WorkerError;
use crate::job::JobId;

/// Most jobs one [`jobs`] call returns.
pub const MAX_JOBS_LISTED: u32 = 500;

/// Where a job is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobState {
    /// Waiting for a worker.
    Queued,
    /// A worker holds it and drives its run.
    Running,
    /// An attempt ended without finishing it (an error, or a busy
    /// session); it is tried again later. `error` says why.
    Retrying,
    /// It can never succeed; it is kept with its `error` and never tried
    /// again, until [`prune_history`] deletes it with the history.
    Failed,
    /// Its run ended, or suspended for an approval or an answer.
    Done,
    /// Another owner claimed its run, which goes on there.
    Superseded,
}

impl JobState {
    fn parse(text: &str) -> Self {
        match text {
            "running" => Self::Running,
            "retrying" => Self::Retrying,
            "failed" => Self::Failed,
            "done" => Self::Done,
            "superseded" => Self::Superseded,
            _ => Self::Queued,
        }
    }

    /// The state's name: `queued`, `running`, `retrying`, `failed`, `done`
    /// or `superseded`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Retrying => "retrying",
            Self::Failed => "failed",
            Self::Done => "done",
            Self::Superseded => "superseded",
        }
    }
}

/// A job and what became of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobInfo {
    /// Its id.
    pub id: JobId,
    /// Its queue.
    pub queue: String,
    /// Its namespace.
    pub namespace: String,
    /// Where it is.
    pub state: JobState,
    /// The session of its run, for a job that starts one.
    pub session_id: Option<SessionId>,
    /// Its run, once a worker named it.
    pub run_id: Option<RunId>,
    /// The schedule that made it, if one did.
    pub schedule_id: Option<String>,
    /// The fire time of that schedule it was made for.
    pub fire_at: Option<DateTime<Utc>>,
    /// How many times a worker took it.
    pub attempts: u32,
    /// How many of those attempts failed.
    pub failures: u32,
    /// The last attempt's error, while retrying or failed.
    pub error: Option<String>,
    /// When it was queued.
    pub created_at: DateTime<Utc>,
    /// When it ended (done, superseded or failed).
    pub finished_at: Option<DateTime<Utc>>,
}

type Row = (
    i64,
    String,
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<DateTime<Utc>>,
    i32,
    i32,
    Option<String>,
    DateTime<Utc>,
    Option<DateTime<Utc>>,
);

fn info(row: Row) -> JobInfo {
    let count = |n: i32| u32::try_from(n).unwrap_or(0);
    JobInfo {
        id: JobId(row.0),
        queue: row.1,
        namespace: row.2,
        state: JobState::parse(&row.3),
        session_id: row.4.map(SessionId::new),
        run_id: row.5.map(RunId::new),
        schedule_id: row.6,
        fire_at: row.7,
        attempts: count(row.8),
        failures: count(row.9),
        error: row.10,
        created_at: row.11,
        finished_at: row.12,
    }
}

/// Jobs of `namespace` (`$1`), live and ended, as [`Row`]s; `$2` filters
/// by job id and `$3` by schedule when not NULL.
const SELECT: &str = "SELECT * FROM ( \
     SELECT id, queue, namespace, \
            CASE WHEN failed_at IS NOT NULL THEN 'failed' \
                 WHEN owner IS NOT NULL AND visible_at > now() THEN 'running' \
                 WHEN error IS NOT NULL THEN 'retrying' \
                 ELSE 'queued' END, \
            session_id, run_id, schedule_id, fire_at, attempts, failures, error, created_at, \
            failed_at \
     FROM linch_worker_jobs \
     WHERE namespace = $1 AND ($2::bigint IS NULL OR id = $2) \
       AND ($3::text IS NULL OR schedule_id = $3) \
     UNION ALL \
     SELECT id, queue, namespace, outcome, session_id, run_id, schedule_id, fire_at, attempts, \
            0, NULL, created_at, finished_at \
     FROM linch_worker_history \
     WHERE namespace = $1 AND ($2::bigint IS NULL OR id = $2) \
       AND ($3::text IS NULL OR schedule_id = $3)) AS j \
     ORDER BY id DESC LIMIT $4";

/// One job of `namespace`, live or ended, or `None` if it has no such
/// job (or its history was pruned).
///
/// Raises:
///     WorkerError: If the query fails.
pub async fn job(
    pool: &PgPool,
    namespace: &str,
    id: JobId,
) -> Result<Option<JobInfo>, WorkerError> {
    let row: Option<Row> = sqlx::query_as(SELECT)
        .bind(namespace)
        .bind(Some(id.0))
        .bind(None::<String>)
        .bind(1_i64)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(info))
}

/// The jobs of `namespace`, newest first: queued, running, retrying and
/// failed ones, and the history of ended ones.
///
/// Args:
///     pool: Pool for the queue's database.
///     namespace: The namespace whose jobs are listed.
///     schedule_id: Only the jobs this schedule made, when given.
///     limit: The most jobs returned, at most [`MAX_JOBS_LISTED`].
///
/// Raises:
///     WorkerError: If the query fails.
pub async fn jobs(
    pool: &PgPool,
    namespace: &str,
    schedule_id: Option<&str>,
    limit: u32,
) -> Result<Vec<JobInfo>, WorkerError> {
    let rows: Vec<Row> = sqlx::query_as(SELECT)
        .bind(namespace)
        .bind(None::<i64>)
        .bind(schedule_id)
        .bind(i64::from(limit.min(MAX_JOBS_LISTED)))
        .fetch_all(pool)
        .await?;
    Ok(rows.into_iter().map(info).collect())
}

/// Delete the history of jobs that ended more than `older_than` ago, and
/// the failed jobs (dead letters, never run again) that failed more than
/// `older_than` ago: each is readable with [`job`] and [`jobs`] for as long
/// as the other.
///
/// Returns:
///     How many history rows and failed jobs were deleted.
///
/// Raises:
///     WorkerError: If a delete fails.
pub async fn prune_history(pool: &PgPool, older_than: Duration) -> Result<u64, WorkerError> {
    let millis = i64::try_from(older_than.as_millis()).unwrap_or(i64::MAX);
    let history = sqlx::query(
        "DELETE FROM linch_worker_history \
         WHERE finished_at < now() - $1 * interval '1 millisecond'",
    )
    .bind(millis)
    .execute(pool)
    .await?;
    let failed = sqlx::query(
        "DELETE FROM linch_worker_jobs \
         WHERE failed_at < now() - $1 * interval '1 millisecond'",
    )
    .bind(millis)
    .execute(pool)
    .await?;
    Ok(history.rows_affected() + failed.rows_affected())
}
