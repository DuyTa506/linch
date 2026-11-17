//! [`PgToolCache`]: cached tool results in Postgres, shared by every server
//! of an agent's namespace.
//!
//! Each call is one statement in autocommit. Expiry is the server's clock
//! (`now()`), so processes with different clocks agree on when an entry
//! ended. A `put` replaces the entry under its key (it becomes the newest)
//! and deletes the namespace's expired and surplus entries in the same
//! statement.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{ToolCacheError, ToolCacheStore, ToolOutput};
use sqlx::{PgPool, Row};

/// The live entry under `($1 namespace, $2 key)`.
const GET: &str = "SELECT output FROM linch_tool_cache \
     WHERE namespace = $1 AND key = $2 AND expires_at > now()";

/// Store or replace an entry, then trim the namespace to `$5` live entries:
/// the new one and the newest `$5 - 1` others (the delete does not see the
/// upsert, so it keeps one less). The upsert's new `seq` makes the entry the
/// newest.
const PUT: &str = "WITH put AS ( \
         INSERT INTO linch_tool_cache (namespace, key, output, expires_at) \
         VALUES ($1, $2, $3, now() + make_interval(secs => $4)) \
         ON CONFLICT (namespace, key) DO UPDATE \
         SET output = EXCLUDED.output, expires_at = EXCLUDED.expires_at, \
             seq = DEFAULT \
         RETURNING key \
     ) \
     DELETE FROM linch_tool_cache \
     WHERE namespace = $1 AND key <> $2 AND ( \
         expires_at <= now() \
         OR key NOT IN ( \
             SELECT key FROM linch_tool_cache \
             WHERE namespace = $1 AND key <> $2 AND expires_at > now() \
             ORDER BY seq DESC LIMIT GREATEST($5 - 1, 0) \
         ) \
     )";

/// A [`ToolCacheStore`] backed by Postgres. Run [`crate::migrate`] on the
/// database before the first call.
#[derive(Debug, Clone)]
pub struct PgToolCache {
    pool: PgPool,
}

impl PgToolCache {
    /// A cache that runs its queries on `pool`.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

fn backend(error: sqlx::Error) -> ToolCacheError {
    ToolCacheError(Box::new(error))
}

#[async_trait]
impl ToolCacheStore for PgToolCache {
    async fn get(&self, namespace: &str, key: &str) -> Result<Option<ToolOutput>, ToolCacheError> {
        let row = sqlx::query(GET)
            .bind(namespace)
            .bind(key)
            .fetch_optional(&self.pool)
            .await
            .map_err(backend)?;
        let Some(row) = row else { return Ok(None) };
        let text: String = row.try_get("output").map_err(backend)?;
        let output =
            serde_json::from_str(&text).map_err(|error| ToolCacheError(Box::new(error)))?;
        Ok(Some(output))
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        ttl: Duration,
        max_entries: u32,
    ) -> Result<(), ToolCacheError> {
        let text =
            serde_json::to_string(output).map_err(|error| ToolCacheError(Box::new(error)))?;
        sqlx::query(PUT)
            .bind(namespace)
            .bind(key)
            .bind(text)
            .bind(ttl.as_secs_f64())
            .bind(i64::from(max_entries))
            .execute(&self.pool)
            .await
            .map_err(backend)?;
        Ok(())
    }

    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError> {
        sqlx::query("DELETE FROM linch_tool_cache WHERE namespace = $1")
            .bind(namespace)
            .execute(&self.pool)
            .await
            .map_err(backend)?;
        Ok(())
    }
}
