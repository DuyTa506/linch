//! Stored agent definitions: the config text a job or schedule runs with,
//! keyed by its digest, and the per-scope caps on what a namespace root
//! may hold.

use sqlx::{Postgres, Transaction};

use crate::error::WorkerError;

/// Store `config` as a definition of `namespace` inside `tx`, unless it
/// is stored already, and return its digest (SHA-256 of the text, hex).
pub(crate) async fn store(
    tx: &mut Transaction<'static, Postgres>,
    namespace: &str,
    config: &str,
) -> Result<String, WorkerError> {
    let digest: String = sqlx::query_scalar(
        "WITH d AS (SELECT encode(sha256(convert_to($2, 'UTF8')), 'hex') AS digest), \
              i AS (INSERT INTO linch_worker_agents (namespace, digest, config) \
                    SELECT $1, digest, $2 FROM d ON CONFLICT DO NOTHING) \
         SELECT digest FROM d",
    )
    .bind(namespace)
    .bind(config)
    .fetch_one(&mut **tx)
    .await?;
    Ok(digest)
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
