//! Enqueueing jobs, and the approval wire form an approve job carries.

use std::collections::BTreeMap;

use linch_core::hooks::ApprovalResponse;
use linch_core::{RunId, SessionId};
use linch_postgres::PgPool;
use serde::{Deserialize, Serialize};
use sqlx::{Postgres, Transaction};

use crate::CHANNEL;
use crate::error::{WorkerError, non_blank};

/// Id of a queued job (the `linch_worker_jobs` primary key).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct JobId(pub i64);

/// The host's answer for one pending tool call, as an approve job stores it.
///
/// It maps one to one onto [`ApprovalResponse`], which has no serde form.
/// `Allow {}` is a struct variant so that decoding rejects unknown fields
/// on it too (serde ignores them on a unit variant of a tagged enum).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case", deny_unknown_fields)]
pub enum Approval {
    /// Approve the call.
    Allow {},
    /// Deny the call.
    Deny {
        /// Shown to the model.
        reason: String,
    },
}

impl From<Approval> for ApprovalResponse {
    fn from(approval: Approval) -> Self {
        match approval {
            Approval::Allow {} => ApprovalResponse::Allow,
            Approval::Deny { reason } => ApprovalResponse::Deny { reason },
        }
    }
}

/// Approvals keyed by `tool_use_id`. A `BTreeMap` keeps the stored JSON in
/// one key order.
pub(crate) type Approvals = BTreeMap<String, Approval>;

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

/// Insert `job` and notify the workers, inside `tx`; the notification is
/// delivered when `tx` commits. A job whose dedupe key already exists in
/// its namespace is skipped and gives `None`.
pub(crate) async fn insert(
    tx: &mut Transaction<'static, Postgres>,
    job: NewJob<'_>,
) -> Result<Option<JobId>, WorkerError> {
    non_blank(job.queue, "the queue")?;
    non_blank(job.namespace, "the namespace")?;
    let id: Option<i64> = sqlx::query_scalar(
        "INSERT INTO linch_worker_jobs \
         (queue, namespace, session_id, prompt, run_id, approvals, dedupe_key) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         ON CONFLICT (namespace, dedupe_key) DO NOTHING RETURNING id",
    )
    .bind(job.queue)
    .bind(job.namespace)
    .bind(job.session_id)
    .bind(job.prompt)
    .bind(job.run_id)
    .bind(job.approvals)
    .bind(job.dedupe_key)
    .fetch_optional(&mut **tx)
    .await?;
    if id.is_some() {
        sqlx::query("SELECT pg_notify($1, $2)")
            .bind(CHANNEL)
            .bind(job.queue)
            .execute(&mut **tx)
            .await?;
    }
    Ok(id.map(JobId))
}

async fn enqueue(pool: &PgPool, job: NewJob<'_>) -> Result<JobId, WorkerError> {
    let mut tx = pool.begin().await?;
    let id = insert(&mut tx, job).await?;
    tx.commit().await?;
    id.ok_or_else(|| WorkerError::Invalid("a job without a dedupe key was skipped".into()))
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
    enqueue(pool, job).await
}

/// Queue a continuation of an existing run, for example one whose process
/// crashed outside the worker. A run that is terminal, or suspended for
/// approval, ends the job without running.
///
/// Args:
///     pool: Pool for the queue's database.
///     queue: Queue name.
///     namespace: Store namespace of the run.
///     run_id: The run to resume.
///
/// Returns:
///     The new job's id.
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
    enqueue(pool, job).await
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
///     WorkerError: If a name is blank or the insert fails.
pub async fn enqueue_approve(
    pool: &PgPool,
    queue: &str,
    namespace: &str,
    run_id: &RunId,
    approvals: impl IntoIterator<Item = (String, Approval)>,
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
    enqueue(pool, job).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn approvals_round_trip_strictly() {
        let approvals: Approvals = [
            ("t1".to_owned(), Approval::Allow {}),
            (
                "t2".to_owned(),
                Approval::Deny {
                    reason: "no".into(),
                },
            ),
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
        for bad in [
            json!({"t1": {"decision": "maybe"}}),
            json!({"t1": {"decision": "allow", "x": 1}}),
            json!({"t1": {"decision": "deny"}}),
        ] {
            assert!(serde_json::from_value::<Approvals>(bad).is_err());
        }
    }

    #[test]
    fn approvals_map_onto_responses() {
        assert_eq!(
            ApprovalResponse::from(Approval::Allow {}),
            ApprovalResponse::Allow
        );
        assert_eq!(
            ApprovalResponse::from(Approval::Deny { reason: "r".into() }),
            ApprovalResponse::Deny { reason: "r".into() }
        );
    }
}
