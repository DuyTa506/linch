//! Enqueueing jobs.

use std::collections::BTreeMap;

use linch_core::hooks::ApprovalResponse;
use linch_core::{RunId, SessionId};
use linch_postgres::PgPool;
use sqlx::{Postgres, Transaction};

use crate::CHANNEL;
use crate::error::{WorkerError, non_blank};

/// Id of a queued job (the `linch_worker_jobs` primary key).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct JobId(pub i64);

/// Approvals keyed by `tool_use_id`, stored as JSON in the job row in
/// [`ApprovalResponse`]'s wire form. A `BTreeMap` keeps the stored JSON in
/// one key order.
pub(crate) type Approvals = BTreeMap<String, ApprovalResponse>;

/// The columns of a new job row.
#[derive(Default)]
pub(crate) struct NewJob<'a> {
    pub(crate) queue: &'a str,
    pub(crate) namespace: &'a str,
    pub(crate) session_id: Option<&'a str>,
    pub(crate) prompt: Option<&'a str>,
    pub(crate) run_id: Option<&'a str>,
    pub(crate) approvals: Option<String>,
    pub(crate) dedupe_key: Option<String>,
}

/// Most inserts of a job that names its run, when the run's active job
/// ends between a refused insert and the read of its id.
const ENQUEUE_TRIES: u32 = 3;

/// What [`insert`] did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Inserted {
    /// A new job.
    New(JobId),
    /// The job names a run that has this active job; nothing was inserted.
    RunBusy(JobId),
    /// A job with the same dedupe key exists; nothing was inserted.
    Duplicate,
}

/// Insert `job` and notify the workers, inside `tx`; the notification is
/// delivered when `tx` commits.
pub(crate) async fn insert(
    tx: &mut Transaction<'static, Postgres>,
    job: NewJob<'_>,
) -> Result<Inserted, WorkerError> {
    non_blank(job.queue, "the queue")?;
    non_blank(job.namespace, "the namespace")?;
    // A job that names its run gives way to the run's active job; a start
    // job to an earlier job with its dedupe key.
    let sql = if job.run_id.is_some() {
        "INSERT INTO linch_worker_jobs \
         (queue, namespace, session_id, prompt, run_id, approvals, dedupe_key) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (namespace, run_id) WHERE failed_at IS NULL DO NOTHING RETURNING id"
    } else {
        "INSERT INTO linch_worker_jobs \
         (queue, namespace, session_id, prompt, run_id, approvals, dedupe_key) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (namespace, dedupe_key) DO NOTHING RETURNING id"
    };
    for _ in 0..ENQUEUE_TRIES {
        let id: Option<i64> = sqlx::query_scalar(sql)
            .bind(job.queue)
            .bind(job.namespace)
            .bind(job.session_id)
            .bind(job.prompt)
            .bind(job.run_id)
            .bind(&job.approvals)
            .bind(&job.dedupe_key)
            .fetch_optional(&mut **tx)
            .await?;
        if let Some(id) = id {
            sqlx::query("SELECT pg_notify($1, $2)")
                .bind(CHANNEL)
                .bind(job.queue)
                .execute(&mut **tx)
                .await?;
            return Ok(Inserted::New(JobId(id)));
        }
        let Some(run_id) = job.run_id else {
            return Ok(Inserted::Duplicate);
        };
        let active: Option<i64> = sqlx::query_scalar(
            "SELECT id FROM linch_worker_jobs \
             WHERE namespace = $1 AND run_id = $2 AND failed_at IS NULL",
        )
        .bind(job.namespace)
        .bind(run_id)
        .fetch_optional(&mut **tx)
        .await?;
        if let Some(id) = active {
            return Ok(Inserted::RunBusy(JobId(id)));
        }
        // The run's active job ended in between: try again.
    }
    let run_id = job.run_id.unwrap_or_default();
    Err(WorkerError::Invalid(format!(
        "the active job of run {run_id} kept changing; enqueue again"
    )))
}

async fn enqueue(pool: &PgPool, job: NewJob<'_>) -> Result<Inserted, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    let inserted = insert(&mut tx, job).await?;
    tx.commit().await?;
    Ok(inserted)
}

/// The id of a job enqueued without a dedupe key: the new job, or the
/// active job of its run.
fn job_id(inserted: Inserted) -> Result<JobId, WorkerError> {
    match inserted {
        Inserted::New(id) | Inserted::RunBusy(id) => Ok(id),
        Inserted::Duplicate => Err(WorkerError::Invalid(
            "a job without a dedupe key was skipped".into(),
        )),
    }
}

/// Queue a new run of `prompt` in `session_id`.
///
/// The worker that takes the job starts the run with
/// [`linch_core::Agent::run`] and records its run id on the job before the
/// run's first write, so a retry after a crash continues that run.
///
/// Args:
///     pool: Pool for the queue's database.
///     queue: Queue name; workers dequeue from the queues they serve.
///     namespace: Store namespace of the agent that runs the job.
///     session_id: Session the run belongs to; created if missing.
///     prompt: The user prompt.
///
/// Returns:
///     The new job's id.
///
/// Raises:
///     WorkerError: If a name is blank or the insert fails.
pub async fn enqueue_run(
    pool: &PgPool,
    queue: &str,
    namespace: &str,
    session_id: &SessionId,
    prompt: &str,
) -> Result<JobId, WorkerError> {
    let job = NewJob {
        queue,
        namespace,
        session_id: Some(session_id.as_str()),
        prompt: Some(prompt),
        ..NewJob::default()
    };
    job_id(enqueue(pool, job).await?)
}

/// Queue a continuation of an existing run, for example one whose process
/// crashed outside the worker. A run that is terminal, or suspended for
/// approval, ends the job without running.
///
/// A run has at most one active job. When it has one already, that job
/// continues the run, and its id is returned instead of a new one.
///
/// Args:
///     pool: Pool for the queue's database.
///     queue: Queue name.
///     namespace: Store namespace of the run.
///     run_id: The run to resume.
///
/// Returns:
///     The id of the new job, or of the run's active job.
///
/// Raises:
///     WorkerError: If a name is blank or the insert fails.
pub async fn enqueue_resume(
    pool: &PgPool,
    queue: &str,
    namespace: &str,
    run_id: &RunId,
) -> Result<JobId, WorkerError> {
    let job = NewJob {
        queue,
        namespace,
        run_id: Some(run_id.as_str()),
        ..NewJob::default()
    };
    job_id(enqueue(pool, job).await?)
}

/// Queue the host's answers for a run suspended for approval; the worker
/// continues it with [`linch_core::Agent::approve`].
///
/// Args:
///     pool: Pool for the queue's database.
///     queue: Queue name.
///     namespace: Store namespace of the run.
///     run_id: The suspended run.
///     approvals: The answer per pending `tool_use_id`; see
///         [`linch_core::Agent::approve`] for unanswered and unknown ids.
///
/// Returns:
///     The new job's id.
///
/// Raises:
///     WorkerError: `RunBusy` if the run already has an active job, which
///         may end without reading these answers (enqueue them again once
///         it has ended); otherwise if a name is blank or the insert fails.
pub async fn enqueue_approve(
    pool: &PgPool,
    queue: &str,
    namespace: &str,
    run_id: &RunId,
    approvals: impl IntoIterator<Item = (String, ApprovalResponse)>,
) -> Result<JobId, WorkerError> {
    let approvals: Approvals = approvals.into_iter().collect();
    let approvals = serde_json::to_string(&approvals)
        .map_err(|error| WorkerError::Invalid(format!("approvals: {error}")))?;
    let job = NewJob {
        queue,
        namespace,
        run_id: Some(run_id.as_str()),
        approvals: Some(approvals),
        ..NewJob::default()
    };
    match enqueue(pool, job).await? {
        Inserted::RunBusy(job) => Err(WorkerError::RunBusy {
            run_id: run_id.clone(),
            job,
        }),
        inserted => job_id(inserted),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn approvals_are_stored_in_the_core_wire_form() {
        let deny = ApprovalResponse::Deny {
            reason: "no".into(),
        };
        let approvals: Approvals = [
            ("t1".to_owned(), ApprovalResponse::Allow),
            ("t2".to_owned(), deny),
        ]
        .into();
        let wire = serde_json::to_value(&approvals).unwrap();
        assert_eq!(
            wire,
            json!({"t1": {"decision": "allow"}, "t2": {"decision": "deny", "reason": "no"}})
        );
        assert_eq!(
            serde_json::from_value::<Approvals>(wire).unwrap(),
            approvals
        );
        let bad = json!({"t1": {"decision": "allow", "x": 1}});
        assert!(serde_json::from_value::<Approvals>(bad).is_err());
    }
}
