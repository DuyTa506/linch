//! [`PgSessionStore`]: append-only session logs in Postgres.
//!
//! An append is one statement in autocommit: it moves the session length
//! only from the expected one and inserts the entries from the moved row,
//! so concurrent appends at the same length serialize and all but one get
//! `Conflict`. A refused append reads the length once more to say why.

use async_trait::async_trait;
use linch_core::{LogEntry, SessionId, SessionStore, StoreError};
use sqlx::PgPool;

use crate::codec::{backend, decode_entry, encode_entry, not_found, to_u64};

/// An append in one statement. The length moves only from `$3`; the
/// entries are inserted from the moved row, so they land with it or not at
/// all.
const APPEND: &str = "WITH moved AS ( \
         UPDATE linch_sessions SET len = len + cardinality($4::text[]) \
         WHERE namespace = $1 AND session_id = $2 AND len = $3 RETURNING len \
     ), inserted AS ( \
         INSERT INTO linch_session_entries (namespace, session_id, position, entry) \
         SELECT $1, $2, $3 + n - 1, entry \
         FROM moved, unnest($4::text[]) WITH ORDINALITY AS e(entry, n) \
     ) SELECT len FROM moved";

/// A [`SessionStore`] backed by Postgres; also the
/// [`TaskStore`](linch_core::TaskStore) of the session tasks.
///
/// Logs are keyed by `(namespace, session_id)`. Run [`crate::migrate`] on the
/// database before the first call.
#[derive(Debug, Clone)]
pub struct PgSessionStore {
    pub(crate) pool: PgPool,
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
        let mut cause = String::new();
        for _ in 0..crate::WRITE_TRIES {
            match self
                .write(namespace, session_id, expected_len, &texts)
                .await
            {
                Ok(Some(len)) => return to_u64(len, "len"),
                // `Ok`: the length moved to `expected_len` after the refused write.
                Ok(None) => self.refusal(namespace, session_id, expected_len).await?,
                Err(error) if crate::serialization_failure(&error) => {
                    cause = format!(" (last: {error})");
                }
                Err(error) => return Err(backend(error)),
            }
        }
        Err(StoreError::Conflict {
            message: format!("session {session_id} kept changing during an append{cause}"),
        })
    }
}

impl PgSessionStore {
    /// Run [`APPEND`]; the new length, or `None` when the session refused it.
    async fn write(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        texts: &[String],
    ) -> Result<Option<i64>, sqlx::Error> {
        // No log is longer than i64::MAX; the refusal read names the length.
        let Ok(expected) = i64::try_from(expected_len) else {
            return Ok(None);
        };
        sqlx::query_scalar(APPEND)
            .bind(namespace)
            .bind(session_id.as_str())
            .bind(expected)
            .bind(texts)
            .fetch_optional(&self.pool)
            .await
    }

    /// Why the session refuses an append at `expected_len`, read after a
    /// refused write; `Ok` when that is its length now.
    async fn refusal(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
    ) -> Result<(), StoreError> {
        let len: Option<i64> = sqlx::query_scalar(
            "SELECT len FROM linch_sessions WHERE namespace = $1 AND session_id = $2",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(backend)?;
        let len = len.ok_or_else(|| not_found("session", namespace, session_id.as_str()))?;
        let len = to_u64(len, "len")?;
        if len != expected_len {
            return Err(StoreError::Conflict {
                message: format!("session {session_id} has {len} entries, expected {expected_len}"),
            });
        }
        Ok(())
    }
}
