//! [`PgJournalStore`]: the workflow journal in Postgres.
//!
//! Every write is one statement. A record append locks the workflow row
//! `FOR SHARE` under the lease's epoch, so it either lands before a
//! competing claim (whose update then waits for it, and whose load then
//! sees it) or finds the new epoch and writes nothing. A write that fails
//! with a serialization error runs again (`retried`).

use std::future::Future;

use async_trait::async_trait;
use sqlx::PgPool;

use super::journal::{
    Interrupt, JournalLease, JournalRecord, JournalStore, JournalStoreError, WorkflowRun,
    WorkflowStatus,
};

/// A [`JournalStore`] backed by Postgres.
///
/// Workflows are keyed by `(namespace, workflow_id)`. Run
/// [`crate::migrate`] on the database before the first call. Every query is
/// built at run time, so building this crate never needs a database.
///
/// ```no_run
/// # async fn start(dsn: &str) -> Result<(), Box<dyn std::error::Error>> {
/// let pool = linch_ext::PgPool::connect(dsn).await?;
/// linch_ext::migrate(&pool).await?;
/// let journal = std::sync::Arc::new(linch_ext::workflow::PgJournalStore::new(pool));
/// let options = linch_ext::workflow::WorkflowOptions::new(journal, "tenant-a", "review-42");
/// # Ok(()) }
/// ```
#[derive(Debug, Clone)]
pub struct PgJournalStore {
    pool: PgPool,
}

impl PgJournalStore {
    /// A store that runs its queries on `pool`.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

/// Most times a write runs while it fails with a serialization error, as
/// linch-postgres runs its single-statement writes.
const WRITE_TRIES: u32 = 3;

/// Run `write` again while it fails with a serialization error (40001), up
/// to [`WRITE_TRIES`] times in all.
///
/// Every write is one autocommit statement at the pool's default isolation.
/// Above READ COMMITTED, a statement that waited on a row another
/// transaction then changed fails this way. It wrote nothing, and runs again
/// on a fresh snapshot: a claim then wins, and a write under an older epoch
/// sees the new one and is fenced. Under READ COMMITTED it never happens.
async fn retried<T, F, Fut>(mut write: F) -> Result<T, sqlx::Error>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, sqlx::Error>>,
{
    let mut tries = 1;
    loop {
        match write().await {
            Err(error) if serialization_failure(&error) && tries < WRITE_TRIES => tries += 1,
            outcome => return outcome,
        }
    }
}

fn serialization_failure(error: &sqlx::Error) -> bool {
    let code = error.as_database_error().and_then(|error| error.code());
    code.as_deref() == Some("40001")
}

fn backend(error: impl Into<Box<dyn std::error::Error + Send + Sync>>) -> JournalStoreError {
    JournalStoreError::Backend(error.into())
}

fn to_i64(value: u64, what: &str) -> Result<i64, JournalStoreError> {
    i64::try_from(value).map_err(|_| backend(format!("{what} {value} does not fit a BIGINT")))
}

fn to_u64(value: i64, what: &str) -> Result<u64, JournalStoreError> {
    u64::try_from(value).map_err(|_| backend(format!("stored {what} {value} is negative")))
}

fn status(name: &str, interrupt: Option<String>) -> Result<WorkflowStatus, JournalStoreError> {
    Ok(match (name, interrupt) {
        ("running", None) => WorkflowStatus::Running,
        ("completed", None) => WorkflowStatus::Completed,
        ("failed", None) => WorkflowStatus::Failed,
        ("awaiting_input", Some(text)) => {
            let interrupt: Interrupt = serde_json::from_str(&text).map_err(backend)?;
            WorkflowStatus::AwaitingInput(interrupt)
        }
        (name, _) => return Err(backend(format!("stored status {name} is not valid"))),
    })
}

#[async_trait]
impl JournalStore for PgJournalStore {
    async fn claim(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<JournalLease, JournalStoreError> {
        let epoch: i64 = retried(|| {
            sqlx::query_scalar(
                "INSERT INTO linch_ext_workflows (namespace, workflow_id, status, epoch) \
                 VALUES ($1, $2, 'running', 1) \
                 ON CONFLICT (namespace, workflow_id) DO UPDATE \
                 SET epoch = linch_ext_workflows.epoch + 1, status = 'running', interrupt = NULL \
                 RETURNING epoch",
            )
            .bind(namespace)
            .bind(workflow_id)
            .fetch_one(&self.pool)
        })
        .await
        .map_err(backend)?;
        Ok(JournalLease {
            namespace: namespace.to_owned(),
            workflow_id: workflow_id.to_owned(),
            epoch: to_u64(epoch, "epoch")?,
        })
    }

    async fn load(
        &self,
        namespace: &str,
        workflow_id: &str,
    ) -> Result<Option<WorkflowRun>, JournalStoreError> {
        let row: Option<(String, i64, Option<String>)> = sqlx::query_as(
            "SELECT status, epoch, interrupt FROM linch_ext_workflows \
             WHERE namespace = $1 AND workflow_id = $2",
        )
        .bind(namespace)
        .bind(workflow_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(backend)?;
        let Some((name, epoch, interrupt)) = row else {
            return Ok(None);
        };
        let rows: Vec<(String, i64, String)> = sqlx::query_as(
            "SELECT call_key, occurrence, value FROM linch_ext_workflow_records \
             WHERE namespace = $1 AND workflow_id = $2 \
             ORDER BY call_key COLLATE \"C\", occurrence",
        )
        .bind(namespace)
        .bind(workflow_id)
        .fetch_all(&self.pool)
        .await
        .map_err(backend)?;
        let mut records = Vec::with_capacity(rows.len());
        for (call_key, occurrence, value) in rows {
            records.push(JournalRecord {
                call_key,
                occurrence: to_u64(occurrence, "occurrence")?,
                value: serde_json::from_str(&value).map_err(backend)?,
            });
        }
        Ok(Some(WorkflowRun {
            status: status(&name, interrupt)?,
            epoch: to_u64(epoch, "epoch")?,
            records,
        }))
    }

    async fn append(
        &self,
        lease: &JournalLease,
        record: JournalRecord,
    ) -> Result<(), JournalStoreError> {
        let value = serde_json::to_string(&record.value).map_err(backend)?;
        let epoch = to_i64(lease.epoch, "epoch")?;
        let occurrence = to_i64(record.occurrence, "occurrence")?;
        let (leased, inserted): (i64, i64) = retried(|| {
            sqlx::query_as(
                "WITH lease AS ( \
                     SELECT 1 FROM linch_ext_workflows \
                     WHERE namespace = $1 AND workflow_id = $2 AND epoch = $3 FOR SHARE), \
                 inserted AS ( \
                     INSERT INTO linch_ext_workflow_records \
                         (namespace, workflow_id, call_key, occurrence, value) \
                     SELECT $1, $2, $4, $5, $6 FROM lease \
                     ON CONFLICT DO NOTHING RETURNING 1) \
                 SELECT (SELECT count(*) FROM lease), (SELECT count(*) FROM inserted)",
            )
            .bind(&lease.namespace)
            .bind(&lease.workflow_id)
            .bind(epoch)
            .bind(&record.call_key)
            .bind(occurrence)
            .bind(&value)
            .fetch_one(&self.pool)
        })
        .await
        .map_err(backend)?;
        match (leased, inserted) {
            (0, _) => Err(JournalStoreError::LeaseLost {
                workflow_id: lease.workflow_id.clone(),
                epoch: lease.epoch,
            }),
            (_, 0) => Err(JournalStoreError::Conflict {
                message: format!(
                    "workflow {} already records {} #{}",
                    lease.workflow_id, record.call_key, record.occurrence
                ),
            }),
            _ => Ok(()),
        }
    }

    async fn set_status(
        &self,
        lease: &JournalLease,
        status: WorkflowStatus,
    ) -> Result<(), JournalStoreError> {
        let interrupt = match &status {
            WorkflowStatus::AwaitingInput(interrupt) => {
                Some(serde_json::to_string(interrupt).map_err(backend)?)
            }
            _ => None,
        };
        let epoch = to_i64(lease.epoch, "epoch")?;
        let updated = retried(|| {
            sqlx::query(
                "UPDATE linch_ext_workflows SET status = $4, interrupt = $5 \
                 WHERE namespace = $1 AND workflow_id = $2 AND epoch = $3",
            )
            .bind(&lease.namespace)
            .bind(&lease.workflow_id)
            .bind(epoch)
            .bind(status.name())
            .bind(&interrupt)
            .execute(&self.pool)
        })
        .await
        .map_err(backend)?
        .rows_affected();
        if updated == 0 {
            return Err(JournalStoreError::LeaseLost {
                workflow_id: lease.workflow_id.clone(),
                epoch: lease.epoch,
            });
        }
        Ok(())
    }
}
