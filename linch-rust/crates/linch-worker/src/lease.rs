//! Taking a job off the queue and holding it: dequeue, heartbeat, ack,
//! release and fail. Every write after the dequeue is fenced by the
//! `attempts` value the dequeue set, so a worker that lost its job (another
//! worker dequeued it after the visibility timeout) changes nothing.

use std::time::Duration;

use linch_core::{RunId, SessionId};
use linch_postgres::PgPool;
use tokio::time::Instant;

use crate::error::WorkerError;
use crate::job::{Approvals, JobId};

/// Key space of the per-queue advisory locks that serialize dequeues on a
/// queue with a concurrency limit.
const QUEUE_LOCK: &str = "linch_worker_queue:";

/// What a job asks for, decoded from its row.
#[derive(Debug, Clone)]
pub(crate) struct Job {
    pub(crate) id: JobId,
    pub(crate) namespace: String,
    /// Session and prompt of a start job.
    pub(crate) start: Option<(SessionId, String)>,
    /// The run, once a start job recorded it, or the run to continue.
    pub(crate) run_id: Option<RunId>,
    /// Answers of an approve job; `Err` keeps a row that does not decode.
    pub(crate) approvals: Option<Result<Approvals, String>>,
}

/// A dequeued job and the fencing token of this hold on it.
#[derive(Debug, Clone)]
pub(crate) struct JobLease {
    pub(crate) job: Job,
    /// Local time just before the dequeue was sent: the job is hidden for
    /// at least the timeout from here.
    pub(crate) since: Instant,
    attempts: i32,
}

type JobRow = (
    i64,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    i32,
);

fn millis(duration: Duration) -> i64 {
    i64::try_from(duration.as_millis()).unwrap_or(i64::MAX)
}

/// Take the oldest visible job of `queue` in one of `namespaces`, hiding it
/// for `timeout`. With `limit > 0`, take none while `limit` jobs of the
/// queue are held by live workers anywhere.
pub(crate) async fn dequeue(
    pool: &PgPool,
    queue: &str,
    limit: u32,
    namespaces: &[String],
    owner: &str,
    timeout: Duration,
) -> Result<Option<JobLease>, WorkerError> {
    let since = Instant::now();
    let mut tx = crate::begin(pool).await?;
    if limit > 0 {
        // Serialize the count-then-take of this queue across the fleet.
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
            .bind(format!("{QUEUE_LOCK}{queue}"))
            .execute(&mut *tx)
            .await?;
        let held: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM linch_worker_jobs WHERE queue = $1 \
             AND owner IS NOT NULL AND visible_at > now() AND failed_at IS NULL",
        )
        .bind(queue)
        .fetch_one(&mut *tx)
        .await?;
        if held >= i64::from(limit) {
            tx.commit().await?;
            return Ok(None);
        }
    }
    let row: Option<JobRow> = sqlx::query_as(
        "WITH next AS ( \
             SELECT id FROM linch_worker_jobs \
             WHERE queue = $1 AND namespace = ANY($2) AND failed_at IS NULL \
               AND visible_at <= now() \
             ORDER BY visible_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) \
         UPDATE linch_worker_jobs AS j \
         SET visible_at = now() + $3 * interval '1 millisecond', \
             attempts = j.attempts + 1, owner = $4 \
         FROM next WHERE j.id = next.id \
         RETURNING j.id, j.namespace, j.session_id, j.prompt, j.run_id, j.approvals, j.attempts",
    )
    .bind(queue)
    .bind(namespaces)
    .bind(millis(timeout))
    .bind(owner)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(row.map(|row| decode(row, since)))
}

fn decode(row: JobRow, since: Instant) -> JobLease {
    let (id, namespace, session_id, prompt, run_id, approvals, attempts) = row;
    let start = session_id.zip(prompt).map(|(s, p)| (SessionId::new(s), p));
    let approvals = approvals
        .map(|text| serde_json::from_str::<Approvals>(&text).map_err(|error| error.to_string()));
    let job = Job {
        id: JobId(id),
        namespace,
        start,
        run_id: run_id.map(RunId::new),
        approvals,
    };
    JobLease {
        job,
        since,
        attempts,
    }
}

impl JobLease {
    /// Which dequeue of the job this hold is: 1 for the first.
    pub(crate) fn attempt(&self) -> i32 {
        self.attempts
    }

    /// Hide the job for another `timeout`. `false` means another worker
    /// holds it now.
    pub(crate) async fn heartbeat(
        &self,
        pool: &PgPool,
        timeout: Duration,
    ) -> Result<bool, WorkerError> {
        let done = sqlx::query(
            "UPDATE linch_worker_jobs SET visible_at = now() + $3 * interval '1 millisecond' \
             WHERE id = $1 AND attempts = $2 AND failed_at IS NULL",
        )
        .bind(self.job.id.0)
        .bind(self.attempts)
        .bind(millis(timeout))
        .execute(pool)
        .await?;
        Ok(done.rows_affected() == 1)
    }

    /// Record the run a start job created, before that run's first write.
    pub(crate) async fn record_run(
        &self,
        pool: &PgPool,
        run_id: &RunId,
    ) -> Result<bool, WorkerError> {
        let done =
            sqlx::query("UPDATE linch_worker_jobs SET run_id = $3 WHERE id = $1 AND attempts = $2")
                .bind(self.job.id.0)
                .bind(self.attempts)
                .bind(run_id.as_str())
                .execute(pool)
                .await?;
        Ok(done.rows_affected() == 1)
    }

    /// The job is done: delete it.
    pub(crate) async fn ack(&self, pool: &PgPool) -> Result<bool, WorkerError> {
        let done = sqlx::query("DELETE FROM linch_worker_jobs WHERE id = $1 AND attempts = $2")
            .bind(self.job.id.0)
            .bind(self.attempts)
            .execute(pool)
            .await?;
        Ok(done.rows_affected() == 1)
    }

    /// Give the job back, visible again after `delay`, noting `error`.
    pub(crate) async fn release(
        &self,
        pool: &PgPool,
        delay: Duration,
        error: &str,
    ) -> Result<bool, WorkerError> {
        let done = sqlx::query(
            "UPDATE linch_worker_jobs SET visible_at = now() + $3 * interval '1 millisecond', \
             owner = NULL, error = $4 WHERE id = $1 AND attempts = $2",
        )
        .bind(self.job.id.0)
        .bind(self.attempts)
        .bind(millis(delay))
        .bind(error)
        .execute(pool)
        .await?;
        Ok(done.rows_affected() == 1)
    }

    /// The job cannot succeed: keep it with `error`, never dequeued again.
    pub(crate) async fn fail(&self, pool: &PgPool, error: &str) -> Result<bool, WorkerError> {
        let done = sqlx::query(
            "UPDATE linch_worker_jobs SET failed_at = now(), owner = NULL, error = $3 \
             WHERE id = $1 AND attempts = $2",
        )
        .bind(self.job.id.0)
        .bind(self.attempts)
        .bind(error)
        .execute(pool)
        .await?;
        Ok(done.rows_affected() == 1)
    }
}
