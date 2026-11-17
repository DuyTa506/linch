//! [`PgMemoryStore`]: long-term memories in `linch_memories`, found with
//! Postgres full-text search.
//!
//! Every statement names the namespace and the scope (`scope IS NOT
//! DISTINCT FROM $n`, so the shared scope, a NULL, matches only itself), and
//! binds every value as a parameter: nothing a model wrote is part of the
//! SQL text. A search reads the words [`search_terms`] extracts, joined with
//! `or` for `websearch_to_tsquery('simple', ...)`, which never fails on its
//! input, and ranks with `ts_rank`. The indexed text is the words
//! [`index_words`] extracts, so what is stored is what is asked for. The
//! `simple` configuration does no stemming and drops no stop words, so any
//! language works. No extension is needed.

use async_trait::async_trait;
use linch_core::memory::{Memory, MemoryError, MemoryStore, NewMemory, index_words, search_terms};
use sqlx::{PgConnection, PgPool};

use crate::begin;

/// The columns of a [`Memory`], in the order [`row`] reads them.
const COLUMNS: &str = "id, content, tags, floor(extract(epoch from created_at) * 1000)::bigint";

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
    MemoryError::Backend(Box::new(error))
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
}

impl PgMemoryStore {
    /// A store for `namespace` that runs its queries on `pool`, without a
    /// cap.
    pub fn new(pool: PgPool, namespace: impl Into<String>) -> Self {
        Self {
            pool,
            namespace: namespace.into(),
            cap: None,
        }
    }

    /// This store refusing a new memory once the namespace's root (the
    /// text before its first `/`) holds `max` memories in all its
    /// namespaces and scopes. The count and the insert run under an advisory
    /// lock on the root, so concurrent saves cannot pass the cap together.
    pub fn with_cap(mut self, max: u64) -> Self {
        self.cap = Some(max);
        self
    }

    fn root(&self) -> &str {
        self.namespace.split('/').next().unwrap_or_default()
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
}

#[async_trait]
impl MemoryStore for PgMemoryStore {
    async fn save(&self, scope: Option<&str>, memory: NewMemory) -> Result<Memory, MemoryError> {
        let mut tx = begin(&self.pool).await.map_err(backend)?;
        if let Some(max) = self.cap {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
                .bind(format!("linch_memory_cap:{}", self.root()))
                .execute(&mut *tx)
                .await
                .map_err(backend)?;
            if let Some(existing) = self.stored(&mut tx, scope, &memory.key).await? {
                return Ok(existing);
            }
            let held: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM linch_memories WHERE split_part(namespace, '/', 1) = $1",
            )
            .bind(self.root())
            .fetch_one(&mut *tx)
            .await
            .map_err(backend)?;
            if u64::try_from(held).unwrap_or(0) >= max {
                return Err(MemoryError::LimitReached { max });
            }
        } else if let Some(existing) = self.stored(&mut tx, scope, &memory.key).await? {
            return Ok(existing);
        }
        let mut words = index_words(&memory.content);
        for word in memory.tags.iter().flat_map(|tag| index_words(tag)) {
            if !words.contains(&word) {
                words.push(word);
            }
        }
        let sql = format!(
            "INSERT INTO linch_memories \
             (namespace, id, scope, call_key, content, tags, search) \
             VALUES ($1, $2, $3, $4, $5, $6, to_tsvector('simple', $7)) \
             ON CONFLICT (namespace, call_key) DO NOTHING RETURNING {COLUMNS}"
        );
        let inserted: Option<Row> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(uuid::Uuid::new_v4().simple().to_string())
            .bind(scope)
            .bind(&memory.key)
            .bind(&memory.content)
            .bind(&memory.tags)
            .bind(words.join(" "))
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
    ) -> Result<Vec<Memory>, MemoryError> {
        let terms = search_terms(query);
        if terms.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let sql = format!(
            "SELECT {COLUMNS} FROM linch_memories, \
                 websearch_to_tsquery('simple', $3) AS q \
             WHERE namespace = $1 AND scope IS NOT DISTINCT FROM $2 AND search @@ q \
             ORDER BY ts_rank(search, q) DESC, created_at DESC, id \
             LIMIT $4"
        );
        let limit = i64::try_from(limit).unwrap_or(i64::MAX);
        let found: Vec<Row> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
            .bind(&self.namespace)
            .bind(scope)
            .bind(terms.join(" or "))
            .bind(limit)
            .fetch_all(&self.pool)
            .await
            .map_err(backend)?;
        Ok(found.into_iter().map(row).collect())
    }

    async fn delete(&self, scope: Option<&str>, id: &str) -> Result<bool, MemoryError> {
        let deleted = sqlx::query(
            "DELETE FROM linch_memories \
             WHERE namespace = $1 AND scope IS NOT DISTINCT FROM $2 AND id = $3",
        )
        .bind(&self.namespace)
        .bind(scope)
        .bind(id)
        .execute(&self.pool)
        .await
        .map_err(backend)?;
        Ok(deleted.rows_affected() > 0)
    }
}
