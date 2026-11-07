//! [`PgFileBackend`]: virtual files in Postgres, shared across sessions and
//! processes.
//!
//! An edit locks the file's row with `FOR UPDATE`, applies the edit, then
//! writes it back in one transaction, so concurrent edits of one file
//! serialize and none is lost.

use async_trait::async_trait;
use linch_core::vfs::{FileBackend, VfsError, apply_edit, file_path, normalize_path};
use sqlx::PgPool;

/// A [`FileBackend`] backed by the `linch_files` table.
///
/// Files are keyed by `(namespace, path)`; a backend reads and writes only
/// its own namespace. Run [`crate::migrate`] on the database before the
/// first call. Postgres text cannot hold a NUL character, so writing
/// content that contains one fails with [`VfsError::Backend`].
#[derive(Debug, Clone)]
pub struct PgFileBackend {
    pool: PgPool,
    namespace: String,
}

impl PgFileBackend {
    /// A backend for `namespace` that runs its queries on `pool`.
    pub fn new(pool: PgPool, namespace: impl Into<String>) -> Self {
        Self {
            pool,
            namespace: namespace.into(),
        }
    }
}

fn backend(error: sqlx::Error) -> VfsError {
    VfsError::Backend(error.into())
}

/// `text` with the `LIKE` wildcards and the escape character escaped.
fn escape_like(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        if matches!(c, '\\' | '%' | '_') {
            escaped.push('\\');
        }
        escaped.push(c);
    }
    escaped
}

#[async_trait]
impl FileBackend for PgFileBackend {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        let path = file_path(path)?;
        let content: Option<String> = sqlx::query_scalar(
            "SELECT content FROM linch_files WHERE namespace = $1 AND path = $2",
        )
        .bind(&self.namespace)
        .bind(&path)
        .fetch_optional(&self.pool)
        .await
        .map_err(backend)?;
        content.ok_or(VfsError::NotFound { path })
    }

    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        let path = file_path(path)?;
        sqlx::query(
            "INSERT INTO linch_files (namespace, path, content) VALUES ($1, $2, $3) \
             ON CONFLICT (namespace, path) \
             DO UPDATE SET content = EXCLUDED.content, updated_at = now()",
        )
        .bind(&self.namespace)
        .bind(&path)
        .bind(content)
        .execute(&self.pool)
        .await
        .map_err(backend)?;
        Ok(())
    }

    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError> {
        let path = file_path(path)?;
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        let current: Option<String> = sqlx::query_scalar(
            "SELECT content FROM linch_files WHERE namespace = $1 AND path = $2 FOR UPDATE",
        )
        .bind(&self.namespace)
        .bind(&path)
        .fetch_optional(&mut *tx)
        .await
        .map_err(backend)?;
        let Some(current) = current else {
            return Err(VfsError::NotFound { path });
        };
        let (updated, count) = apply_edit(&path, &current, old, new, replace_all)?;
        sqlx::query(
            "UPDATE linch_files SET content = $3, updated_at = now() \
             WHERE namespace = $1 AND path = $2",
        )
        .bind(&self.namespace)
        .bind(&path)
        .bind(updated)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(count)
    }

    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        // `/a` lists `/a` and `/a/...`; the root (`""` here) lists all.
        let base = normalize_path(prefix)?.trim_end_matches('/').to_owned();
        let below = format!("{}/%", escape_like(&base));
        sqlx::query_scalar(
            "SELECT path FROM linch_files WHERE namespace = $1 \
             AND (path = $2 OR path LIKE $3 ESCAPE '\\') ORDER BY path",
        )
        .bind(&self.namespace)
        .bind(&base)
        .bind(below)
        .fetch_all(&self.pool)
        .await
        .map_err(backend)
    }

    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        let path = file_path(path)?;
        sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM linch_files WHERE namespace = $1 AND path = $2)",
        )
        .bind(&self.namespace)
        .bind(&path)
        .fetch_one(&self.pool)
        .await
        .map_err(backend)
    }

    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        let path = file_path(path)?;
        sqlx::query("DELETE FROM linch_files WHERE namespace = $1 AND path = $2")
            .bind(&self.namespace)
            .bind(&path)
            .execute(&self.pool)
            .await
            .map_err(backend)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::escape_like;

    #[test]
    fn like_wildcards_are_escaped() {
        assert_eq!(escape_like("/a_b%c\\d"), "/a\\_b\\%c\\\\d");
        assert_eq!(escape_like("/plain"), "/plain");
    }
}
