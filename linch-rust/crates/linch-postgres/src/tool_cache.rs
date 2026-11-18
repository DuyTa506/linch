//! [`PgToolCache`]: cached tool results in Postgres, shared by every server
//! of an agent's namespace.
//!
//! Expiry is the server's clock (`now()`), so processes with different
//! clocks agree on when an entry ended.
//!
//! **A stale fill never lands.** A namespace has an epoch that `clear`
//! raises. A `put` and a `clear` of one namespace take the same advisory
//! lock for their whole transaction (shared for a put, exclusive for a
//! clear), and a put checks the epoch it was given against the stored one
//! after taking it, so it either finishes before the clear starts (and the
//! clear deletes it) or sees the raised epoch and stores nothing. The lock
//! is held for one short transaction, never across a tool run.
//!
//! A `put` replaces the entry under its key (it becomes the newest),
//! deletes the namespace's expired and surplus entries, trims the quota's
//! group to its byte cap (newest first), and, after it commits, deletes a
//! bounded batch of expired rows of any namespace, so a namespace nobody
//! uses again does not keep its rows forever.

use async_trait::async_trait;
use linch_core::{ByteQuota, Lookup, PutLimits, ToolCacheError, ToolCacheStore, ToolOutput};
use sqlx::{PgPool, Row};

/// The live entry under `($1 namespace, $2 key)` and the namespace's epoch,
/// from one snapshot.
const GET: &str = "SELECT \
         (SELECT output FROM linch_tool_cache \
          WHERE namespace = $1 AND key = $2 AND expires_at > now()) AS output, \
         COALESCE((SELECT epoch FROM linch_tool_cache_epoch WHERE namespace = $1), 0) AS epoch";

/// The advisory lock of `$1` namespace's epoch, for this schema: shared by
/// a put, exclusive for a clear, held to the end of the transaction.
const LOCK_SHARED: &str = "SELECT pg_advisory_xact_lock_shared(hashtextextended(\
     'linch_tool_cache:' || coalesce(current_schema(), '') || ':' || $1, 0))";
const LOCK_EXCLUSIVE: &str = "SELECT pg_advisory_xact_lock(hashtextextended(\
     'linch_tool_cache:' || coalesce(current_schema(), '') || ':' || $1, 0))";

/// The namespace's epoch, once its lock is held.
const EPOCH: &str = "SELECT COALESCE((SELECT epoch FROM linch_tool_cache_epoch \
     WHERE namespace = $1), 0)";

/// Store or replace an entry, then trim the namespace to `$5` live entries:
/// the new one and the newest `$5 - 1` others (the delete does not see the
/// upsert, so it keeps one less). The upsert's new `seq` makes the entry the
/// newest.
const PUT: &str = "WITH put AS ( \
         INSERT INTO linch_tool_cache \
             (namespace, key, output, bytes, quota_group, expires_at) \
         VALUES ($1, $2, $3, $6, $7, now() + make_interval(secs => $4)) \
         ON CONFLICT (namespace, key) DO UPDATE \
         SET output = EXCLUDED.output, bytes = EXCLUDED.bytes, \
             quota_group = EXCLUDED.quota_group, \
             expires_at = EXCLUDED.expires_at, seq = DEFAULT \
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

/// Delete the quota group's entries past `$2` bytes, counting from the
/// newest.
const QUOTA: &str = "DELETE FROM linch_tool_cache AS c USING ( \
         SELECT namespace, key FROM ( \
             SELECT namespace, key, \
                    sum(bytes) OVER (ORDER BY seq DESC) AS running \
             FROM linch_tool_cache \
             WHERE quota_group = $1 AND expires_at > now() \
         ) AS counted WHERE running > $2::bigint \
     ) AS over \
     WHERE c.namespace = over.namespace AND c.key = over.key";

/// Most expired rows one put deletes besides its own namespace's.
const SWEEP: &str = "DELETE FROM linch_tool_cache \
     WHERE (namespace, key) IN ( \
         SELECT namespace, key FROM linch_tool_cache \
         WHERE expires_at < now() LIMIT 100 \
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

fn json(error: serde_json::Error) -> ToolCacheError {
    ToolCacheError(Box::new(error))
}

/// An epoch as the column holds it. It counts clears, so it never
/// approaches the limit.
fn column(epoch: u64) -> i64 {
    i64::try_from(epoch).unwrap_or(i64::MAX)
}

#[async_trait]
impl ToolCacheStore for PgToolCache {
    async fn get(&self, namespace: &str, key: &str) -> Result<Lookup, ToolCacheError> {
        let row = sqlx::query(GET)
            .bind(namespace)
            .bind(key)
            .fetch_one(&self.pool)
            .await
            .map_err(backend)?;
        let text: Option<String> = row.try_get("output").map_err(backend)?;
        let epoch: i64 = row.try_get("epoch").map_err(backend)?;
        let hit = text
            .map(|text| serde_json::from_str(&text).map_err(json))
            .transpose()?;
        Ok(Lookup {
            hit,
            epoch: u64::try_from(epoch).unwrap_or(0),
        })
    }

    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        epoch: u64,
        limits: &PutLimits,
    ) -> Result<bool, ToolCacheError> {
        let text = serde_json::to_string(output).map_err(json)?;
        let bytes = i64::try_from(text.len()).unwrap_or(i64::MAX);
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        sqlx::query(LOCK_SHARED)
            .bind(namespace)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        let current: i64 = sqlx::query_scalar(EPOCH)
            .bind(namespace)
            .fetch_one(&mut *tx)
            .await
            .map_err(backend)?;
        if current != column(epoch) {
            // A clear since the lookup: the result may predate a write.
            tx.rollback().await.map_err(backend)?;
            return Ok(false);
        }
        let group = limits.quota.as_ref().map(|quota| quota.group.as_str());
        sqlx::query(PUT)
            .bind(namespace)
            .bind(key)
            .bind(text)
            .bind(limits.ttl.as_secs_f64())
            .bind(i64::from(limits.max_entries))
            .bind(bytes)
            .bind(group)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        if let Some(ByteQuota { group, max_bytes }) = &limits.quota {
            sqlx::query(QUOTA)
                .bind(group)
                .bind(i64::try_from(*max_bytes).unwrap_or(i64::MAX))
                .execute(&mut *tx)
                .await
                .map_err(backend)?;
        }
        tx.commit().await.map_err(backend)?;
        // Housekeeping: a failure here loses nothing the put stored.
        let _ = sqlx::query(SWEEP).execute(&self.pool).await;
        Ok(true)
    }

    async fn clear(&self, namespace: &str) -> Result<(), ToolCacheError> {
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        sqlx::query(LOCK_EXCLUSIVE)
            .bind(namespace)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        sqlx::query(
            "INSERT INTO linch_tool_cache_epoch (namespace, epoch) VALUES ($1, 1) \
             ON CONFLICT (namespace) DO UPDATE \
             SET epoch = linch_tool_cache_epoch.epoch + 1",
        )
        .bind(namespace)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        sqlx::query("DELETE FROM linch_tool_cache WHERE namespace = $1")
            .bind(namespace)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(())
    }
}
