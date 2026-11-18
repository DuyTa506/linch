//! [`PgMemoryStore`]: long-term memories in `linch_memories`, found by the
//! words they hold.
//!
//! Every statement names the namespace and the scope (`scope IS NOT
//! DISTINCT FROM $n`, so the shared scope, a NULL, matches only itself), and
//! binds every value as a parameter: nothing a model wrote is part of the
//! SQL text. A row stores the words [`index_words`] extracts, each prefixed
//! with a bucket of its namespace and scope; a search asks for the
//! [`search_terms`] words in its own bucket (`terms && $words`, a GIN scan),
//! counts how many of them a row has, and ranks by that count. Because the
//! prefix is part of the indexed word, the index scan of a common word in
//! one namespace never reads another namespace's postings. The exact
//! namespace and scope predicates stay in the query, so the prefix is a
//! speed-up only. No extension is needed.
//!
//! Every statement runs in a transaction with a `statement_timeout`
//! ([`PgMemoryStore::with_statement_timeout`]), so a slow query or a wait on
//! the cap's lock ends with an error instead of holding a connection.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::memory::{
    Memory, MemoryError, MemoryStore, NewMemory, default_scope_cap, index_words, search_terms,
};
use sha2::{Digest, Sha256};
use sqlx::{PgConnection, PgPool, Postgres, Transaction};

use crate::begin;

/// The columns of a [`Memory`], in the order [`row`] reads them.
const COLUMNS: &str = "id, content, tags, floor(extract(epoch from created_at) * 1000)::bigint";

/// How long one statement may run unless [`PgMemoryStore::with_statement_timeout`]
/// says otherwise.
pub const DEFAULT_STATEMENT_TIMEOUT: Duration = Duration::from_secs(2);

type Row = (String, String, Vec<String>, i64);

fn row((id, content, tags, created): Row) -> Memory {
    Memory {
        id,
        content,
        tags,
        created_at_ms: u64::try_from(created).unwrap_or(0),
    }
}

fn backend(error: sqlx::Error) -> MemoryError {
    MemoryError::Backend(crate::exhausted::backend_source(error))
}

/// The prefix of every indexed word of one namespace and scope: 16 hex
/// digits of a hash that cannot be confused across the two parts.
fn bucket(namespace: &str, scope: Option<&str>) -> String {
    let mut digest = Sha256::new();
    digest.update(namespace.as_bytes());
    match scope {
        None => digest.update([0]),
        Some(scope) => {
            digest.update([1]);
            digest.update(scope.as_bytes());
        }
    }
    let digest = digest.finalize();
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// A [`MemoryStore`] backed by Postgres, for the agents of one namespace.
///
/// Rows are keyed by `(namespace, id)`, so two tenants never see each
/// other's memories. Run [`crate::migrate`] on the database before the
/// first call.
#[derive(Debug, Clone)]
pub struct PgMemoryStore {
    pool: PgPool,
    namespace: String,
    cap: Option<u64>,
    scope_cap: Option<u64>,
    timeout: Duration,
}

impl PgMemoryStore {
    /// A store for `namespace` that runs its queries on `pool`, without a
    /// cap.
    pub fn new(pool: PgPool, namespace: impl Into<String>) -> Self {
        Self {
            pool,
            namespace: namespace.into(),
            cap: None,
            scope_cap: None,
            timeout: DEFAULT_STATEMENT_TIMEOUT,
        }
    }

    /// This store refusing a new memory once the namespace's root (the
    /// text before its first `/`) holds `max` memories in all its
    /// namespaces and scopes. Each scope is held to [`default_scope_cap`]
    /// of it, unless [`Self::with_scope_cap`] names another. The counts and
    /// the insert run under an advisory lock on the root, so concurrent
    /// saves cannot pass a cap together; a count stops at the cap, so it
    /// never reads more rows than the cap allows.
    pub fn with_cap(mut self, max: u64) -> Self {
        self.cap = Some(max);
        self
    }

    /// This store refusing a new memory once a scope of the root holds
    /// `max` of its own. It counts only with [`Self::with_cap`].
    pub fn with_scope_cap(mut self, max: u64) -> Self {
        self.scope_cap = Some(max);
        self
    }

    /// This store's statements failing after `timeout` (rounded up to a
    /// millisecond) instead of the default
    /// ([`DEFAULT_STATEMENT_TIMEOUT`]).
    pub fn with_statement_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    fn root(&self) -> &str {
        self.namespace.split('/').next().unwrap_or_default()
    }

    /// A transaction whose statements are cut off at the store's timeout.
    async fn timed(&self) -> Result<Transaction<'static, Postgres>, MemoryError> {
        let mut tx = begin(&self.pool).await.map_err(backend)?;
        let millis = self.timeout.as_millis().max(1).to_string();
        sqlx::query("SELECT set_config('statement_timeout', $1, true)")
            .bind(millis)
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        Ok(tx)
    }

    /// The words of `memory`, each in this store's bucket for `scope`.
    fn terms(&self, scope: Option<&str>, memory: &NewMemory) -> Vec<String> {
        let mut words = index_words(&memory.content);
        for word in memory.tags.iter().flat_map(|tag| index_words(tag)) {
            if !words.contains(&word) {
                words.push(word);
            }
        }
        let prefix = bucket(&self.namespace, scope);
        words.into_iter().map(|w| format!("{prefix}{w}")).collect()
    }

    async fn stored(
        &self,
        conn: &mut PgConnection,
        scope: Option<&str>,
        key: &str,
    ) -> Result<Option<Memory>, MemoryError> {
        let sql = format!(
            "SELECT {COLUMNS} FROM linch_memories \
             WHERE namespace = $1 AND call_key = $2 AND scope IS NOT DISTINCT FROM $3"
        );
        let found: Option<Row> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(key)
            .bind(scope)
            .fetch_optional(conn)
            .await
            .map_err(backend)?;
        Ok(found.map(row))
    }

    /// Whether the root already holds `max` rows (of `scope` when it is
    /// named); reads at most `max` of them.
    async fn full(
        &self,
        conn: &mut PgConnection,
        scope: Option<Option<&str>>,
        max: u64,
    ) -> Result<bool, MemoryError> {
        let filter = match scope {
            None => "",
            Some(Some(_)) => " AND scope = $3",
            Some(None) => " AND scope IS NULL",
        };
        let sql = format!(
            "SELECT count(*) FROM (SELECT 1 FROM linch_memories \
             WHERE split_part(namespace, '/', 1) = $1{filter} LIMIT $2) AS held"
        );
        let mut query = sqlx::query_scalar(sqlx::AssertSqlSafe(sql))
            .bind(self.root())
            .bind(i64::try_from(max).unwrap_or(i64::MAX));
        if let Some(Some(scope)) = scope {
            query = query.bind(scope);
        }
        let held: i64 = query.fetch_one(conn).await.map_err(backend)?;
        Ok(u64::try_from(held).unwrap_or(0) >= max)
    }
}

#[async_trait]
impl MemoryStore for PgMemoryStore {
    async fn save(&self, scope: Option<&str>, memory: NewMemory) -> Result<Memory, MemoryError> {
        let mut tx = self.timed().await?;
        if let Some(max) = self.cap {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
                .bind(format!("linch_memory_cap:{}", self.root()))
                .execute(&mut *tx)
                .await
                .map_err(backend)?;
            if let Some(existing) = self.stored(&mut tx, scope, &memory.key).await? {
                return Ok(existing);
            }
            let each = self.scope_cap.unwrap_or_else(|| default_scope_cap(max));
            if self.full(&mut tx, Some(scope), each).await? {
                let per_scope = true;
                return Err(MemoryError::LimitReached {
                    max: each,
                    per_scope,
                });
            }
            if self.full(&mut tx, None, max).await? {
                let per_scope = false;
                return Err(MemoryError::LimitReached { max, per_scope });
            }
        } else if let Some(existing) = self.stored(&mut tx, scope, &memory.key).await? {
            return Ok(existing);
        }
        let sql = format!(
            "INSERT INTO linch_memories \
             (namespace, id, scope, call_key, content, tags, terms) \
             VALUES ($1, $2, $3, $4, $5, $6, $7) \
             ON CONFLICT (namespace, call_key) DO NOTHING RETURNING {COLUMNS}"
        );
        let inserted: Option<Row> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(uuid::Uuid::new_v4().simple().to_string())
            .bind(scope)
            .bind(&memory.key)
            .bind(&memory.content)
            .bind(&memory.tags)
            .bind(self.terms(scope, &memory))
            .fetch_optional(&mut *tx)
            .await
            .map_err(backend)?;
        let saved = match inserted {
            Some(inserted) => row(inserted),
            // A concurrent save of the same key won without the cap's lock.
            None => match self.stored(&mut tx, scope, &memory.key).await? {
                Some(existing) => existing,
                None => {
                    let message = "the memory key belongs to another scope";
                    return Err(MemoryError::Backend(message.into()));
                }
            },
        };
        tx.commit().await.map_err(backend)?;
        Ok(saved)
    }

    async fn search(
        &self,
        scope: Option<&str>,
        query: &str,
        limit: usize,
        min_match: usize,
    ) -> Result<Vec<Memory>, MemoryError> {
        let terms = search_terms(query);
        if terms.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let prefix = bucket(&self.namespace, scope);
        let asked: Vec<String> = terms.iter().map(|t| format!("{prefix}{t}")).collect();
        // The GIN scan finds the rows that have any asked word in this
        // bucket; each row's hits are counted once, for the filter and the
        // order.
        let sql = format!(
            "SELECT {COLUMNS} FROM linch_memories AS m, \
                 LATERAL (SELECT count(*) AS hits FROM unnest($3::text[]) AS t \
                          WHERE t = ANY(m.terms)) AS h \
             WHERE m.namespace = $1 AND m.scope IS NOT DISTINCT FROM $2 \
               AND m.terms && $3::text[] AND h.hits >= $4 \
             ORDER BY h.hits DESC, m.created_at DESC, m.id \
             LIMIT $5"
        );
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let needed = i64::try_from(min_match.max(1)).unwrap_or(i64::MAX);
        let mut tx = self.timed().await?;
        let found: Vec<Row> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(scope)
            .bind(asked)
            .bind(needed)
            .bind(limit)
            .fetch_all(&mut *tx)
            .await
            .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(found.into_iter().map(row).collect())
    }

    async fn delete(&self, scope: Option<&str>, id: &str) -> Result<bool, MemoryError> {
        let mut tx = self.timed().await?;
        let deleted = sqlx::query(
            "DELETE FROM linch_memories \
             WHERE namespace = $1 AND scope IS NOT DISTINCT FROM $2 AND id = $3",
        )
        .bind(&self.namespace)
        .bind(scope)
        .bind(id)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(deleted.rows_affected() > 0)
    }

    async fn purge(&self, scope: Option<&str>) -> Result<u64, MemoryError> {
        let mut tx = self.timed().await?;
        let deleted = sqlx::query(
            "DELETE FROM linch_memories WHERE namespace = $1 AND scope IS NOT DISTINCT FROM $2",
        )
        .bind(&self.namespace)
        .bind(scope)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(deleted.rows_affected())
    }
}
