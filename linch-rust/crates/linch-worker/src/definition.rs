//! Stored agent definitions: the config text a job or schedule runs with,
//! keyed by its digest, and the per-scope caps on what a namespace root
//! may hold.

use linch_postgres::PgPool;
use sqlx::{Postgres, Transaction};

use crate::error::WorkerError;

/// Store `config` as a definition of `namespace` inside `tx`, unless it
/// is stored already, and return its digest (SHA-256 of the text, hex).
/// The row stays locked until `tx` ends, so [`prune_definitions`] skips a
/// definition that is about to be named.
pub(crate) async fn store(
    tx: &mut Transaction<'static, Postgres>,
    namespace: &str,
    config: &str,
) -> Result<String, WorkerError> {
    let digest: String = sqlx::query_scalar(
        "WITH d AS (SELECT encode(sha256(convert_to($2, 'UTF8')), 'hex') AS digest), \
              i AS (INSERT INTO linch_worker_agents (namespace, digest, config) \
                    SELECT $1, digest, $2 FROM d \
                    ON CONFLICT (namespace, digest) DO UPDATE SET config = EXCLUDED.config) \
         SELECT digest FROM d",
    )
    .bind(namespace)
    .bind(config)
    .fetch_one(&mut **tx)
    .await?;
    Ok(digest)
}

/// Delete the stored definitions that no schedule, no unfinished job and
/// no run that is not terminal names, so the config secrets they hold (MCP
/// env and header values) live no longer than their schedules and runs. A
/// failed job is never run again, so it lets go of its definition first. A
/// run suspended for an approval or an answer keeps its job's definition
/// through the job's history row, for the job that continues it, until
/// that row is pruned. A worker calls this with [`crate::prune_history`].
///
/// A definition a writer is storing right now is locked by that writer
/// and skipped; one a writer stored after this call read the references
/// makes the delete fail its foreign key, and this call then deletes
/// nothing and returns 0, to try again on the next prune.
///
/// Returns:
///     How many definitions were deleted.
///
/// Raises:
///     WorkerError: If a query fails.
pub async fn prune_definitions(pool: &PgPool) -> Result<u64, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    sqlx::query(
        "UPDATE linch_worker_jobs SET agent = NULL \
         WHERE failed_at IS NOT NULL AND agent IS NOT NULL",
    )
    .execute(&mut *tx)
    .await?;
    let deleted = sqlx::query(
        "DELETE FROM linch_worker_agents WHERE (namespace, digest) IN ( \
             SELECT a.namespace, a.digest FROM linch_worker_agents AS a \
             WHERE NOT EXISTS (SELECT 1 FROM linch_worker_schedules AS s \
                               WHERE s.namespace = a.namespace AND s.agent = a.digest) \
               AND NOT EXISTS (SELECT 1 FROM linch_worker_jobs AS j \
                               WHERE j.namespace = a.namespace AND j.agent = a.digest) \
               AND NOT EXISTS (SELECT 1 FROM linch_worker_history AS h \
                               JOIN linch_runs AS r \
                                 ON r.namespace = h.namespace AND r.run_id = h.run_id \
                               WHERE h.namespace = a.namespace AND h.agent = a.digest \
                                 AND r.status NOT IN ('completed', 'failed', 'aborted')) \
             FOR UPDATE SKIP LOCKED)",
    )
    .execute(&mut *tx)
    .await;
    let deleted = match deleted {
        Ok(deleted) => deleted.rows_affected(),
        Err(error) if foreign_key_violation(&error) => {
            tracing::debug!("a definition was named while pruning; pruning again later");
            return Ok(0);
        }
        Err(error) => return Err(error.into()),
    };
    tx.commit().await?;
    Ok(deleted)
}

fn foreign_key_violation(error: &sqlx::Error) -> bool {
    matches!(error, sqlx::Error::Database(db) if db.code().as_deref() == Some("23503"))
}

/// A cap on what the namespaces under one root may hold: the root is a
/// namespace's first `/`-separated segment (a tenant's name).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cap {
    /// The root counted, such as `acme` for `acme` and `acme/support`.
    pub root: String,
    /// The most rows the root may hold.
    pub max: u64,
}

/// What a [`Cap`] counts.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Counted {
    /// Schedules, paused ones included.
    Schedules,
    /// Jobs that are queued, running or waiting to retry (not failed).
    Jobs,
}

impl Counted {
    fn name(self) -> &'static str {
        match self {
            Self::Schedules => "schedules",
            Self::Jobs => "jobs",
        }
    }
}

/// Refuse with `LimitReached` when `cap`'s root already holds its `max`.
/// Takes a transaction-scoped advisory lock on the root first, so two
/// writers that check at once count each other's rows.
pub(crate) async fn check(
    tx: &mut Transaction<'static, Postgres>,
    cap: Option<&Cap>,
    counted: Counted,
) -> Result<(), WorkerError> {
    let Some(cap) = cap else { return Ok(()) };
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("linch_worker_cap:{}:{}", counted.name(), cap.root))
        .execute(&mut **tx)
        .await?;
    let sql = match counted {
        Counted::Schedules => {
            "SELECT count(*) FROM linch_worker_schedules WHERE split_part(namespace, '/', 1) = $1"
        }
        Counted::Jobs => {
            "SELECT count(*) FROM linch_worker_jobs \
             WHERE split_part(namespace, '/', 1) = $1 AND failed_at IS NULL"
        }
    };
    let held: i64 = sqlx::query_scalar(sql)
        .bind(&cap.root)
        .fetch_one(&mut **tx)
        .await?;
    if u64::try_from(held).unwrap_or(0) >= cap.max {
        return Err(WorkerError::LimitReached {
            what: counted.name(),
            max: cap.max,
        });
    }
    Ok(())
}
