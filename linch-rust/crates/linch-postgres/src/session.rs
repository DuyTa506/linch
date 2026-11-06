//! [`PgSessionStore`]: append-only session logs in Postgres.
//!
//! An append locks the session row, compares its length, then writes the
//! new length and the entries in one transaction, so concurrent appends at
//! the same length serialize and all but one get `Conflict`.

use async_trait::async_trait;
use linch_core::{LogEntry, SessionId, SessionStore, StoreError};
use sqlx::PgPool;

use crate::codec::{backend, decode_entry, encode_entry, not_found, to_i64, to_u64};

/// A [`SessionStore`] backed by Postgres.
///
/// Logs are keyed by `(namespace, session_id)`. Run [`crate::migrate`] on the
/// database before the first call.
#[derive(Debug, Clone)]
pub struct PgSessionStore {
    pool: PgPool,
}

impl PgSessionStore {
    /// A store that runs its queries on `pool`.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

#[async_trait]
impl SessionStore for PgSessionStore {
    async fn create(&self, namespace: &str, session_id: &SessionId) -> Result<(), StoreError> {
        let inserted = sqlx::query(
            "INSERT INTO linch_sessions (namespace, session_id, len) VALUES ($1, $2, 0) \
             ON CONFLICT DO NOTHING",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .execute(&self.pool)
        .await
        .map_err(backend)?
        .rows_affected();
        if inserted == 0 {
            return Err(StoreError::Conflict {
                message: format!("session {session_id} already exists in {namespace}"),
            });
        }
        Ok(())
    }

    async fn load(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<LogEntry>, StoreError> {
        // One row with a NULL entry for an empty log, none for a missing one.
        let rows: Vec<(Option<String>,)> = sqlx::query_as(
            "SELECT e.entry FROM linch_sessions s \
             LEFT JOIN linch_session_entries e \
             ON e.namespace = s.namespace AND e.session_id = s.session_id \
             WHERE s.namespace = $1 AND s.session_id = $2 ORDER BY e.position",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .fetch_all(&self.pool)
        .await
        .map_err(backend)?;
        if rows.is_empty() {
            return Err(not_found("session", namespace, session_id.as_str()));
        }
        rows.into_iter()
            .filter_map(|(text,)| text)
            .map(|text| decode_entry(&text))
            .collect()
    }

    async fn append(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        let texts = entries
            .iter()
            .map(encode_entry)
            .collect::<Result<Vec<_>, _>>()?;
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        let len: Option<i64> = sqlx::query_scalar(
            "SELECT len FROM linch_sessions WHERE namespace = $1 AND session_id = $2 FOR UPDATE",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .fetch_optional(&mut *tx)
        .await
        .map_err(backend)?;
        let len = len.ok_or_else(|| not_found("session", namespace, session_id.as_str()))?;
        let len = to_u64(len, "len")?;
        if len != expected_len {
            return Err(StoreError::Conflict {
                message: format!("session {session_id} has {len} entries, expected {expected_len}"),
            });
        }
        let new_len = len + texts.len() as u64;
        // The session row first, so a crash during the entry insert still
        // has a landed write to roll back.
        sqlx::query("UPDATE linch_sessions SET len = $3 WHERE namespace = $1 AND session_id = $2")
            .bind(namespace)
            .bind(session_id.as_str())
            .bind(to_i64(new_len, "len")?)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        if !texts.is_empty() {
            sqlx::query(
                "INSERT INTO linch_session_entries (namespace, session_id, position, entry) \
                 SELECT $1, $2, $3 + n - 1, entry FROM unnest($4::text[]) \
                 WITH ORDINALITY AS e(entry, n)",
            )
            .bind(namespace)
            .bind(session_id.as_str())
            .bind(to_i64(len, "len")?)
            .bind(texts)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        }
        tx.commit().await.map_err(backend)?;
        Ok(new_len)
    }
}
