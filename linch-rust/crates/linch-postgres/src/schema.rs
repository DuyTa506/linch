//! The schema version gate: what migrations the database has applied, read
//! without writing, against the ones this binary embeds.

use sqlx::{AssertSqlSafe, PgPool};
use thiserror::Error;

use crate::{MIGRATIONS_TABLE, MigrateError, migrator};

/// The newest migration a migration table holds, and the newest this binary
/// knows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchemaVersion {
    /// The newest applied migration; `None` while nothing is applied (a
    /// database never migrated).
    pub applied: Option<i64>,
    /// The newest migration this binary embeds.
    pub latest: i64,
}

impl SchemaVersion {
    /// Whether every migration this binary embeds is applied.
    pub fn is_current(&self) -> bool {
        self.applied == Some(self.latest)
    }
}

/// The schema could not be read, or does not fit this binary.
#[derive(Debug, Error)]
pub enum SchemaError {
    /// The database holds a migration this binary does not embed: a newer
    /// release migrated it. Running on it could write rows this binary
    /// cannot read, so nothing opens.
    #[error(
        "the database schema is newer than this binary: {table} holds migration {applied}, \
         the newest this binary knows is {latest}; upgrade this binary"
    )]
    Newer {
        /// The migration table that holds it.
        table: String,
        /// The newest applied migration.
        applied: i64,
        /// The newest migration this binary embeds.
        latest: i64,
    },
    /// The database could not be read.
    #[error("cannot reach or read the database: {0}")]
    Database(#[from] sqlx::Error),
    /// A migration failed (or an applied one differs from the embedded one).
    #[error("migration failed: {0}")]
    Migrate(#[from] MigrateError),
}

/// What the store migrations have applied in the pool's database and
/// schema, against what this binary embeds. Writes nothing.
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to read.
///
/// Returns:
///     The applied and the latest version; a database never migrated has
///     `applied: None`.
///
/// Raises:
///     SchemaError: `Newer` when the database holds a migration this binary
///         does not embed; `Database` when the read fails.
pub async fn schema_version(pool: &PgPool) -> Result<SchemaVersion, SchemaError> {
    let known: Vec<i64> = migrator()
        .iter()
        .map(|migration| migration.version)
        .collect();
    check_table(pool, MIGRATIONS_TABLE, &known).await
}

/// [`schema_version`] for any migration table, against the `known`
/// versions its binary embeds (the worker crate checks its own table this
/// way).
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to read.
///     table: The migration table.
///     known: Every version the binary embeds.
///
/// Raises:
///     SchemaError: As [`schema_version`].
pub async fn check_table(
    pool: &PgPool,
    table: &str,
    known: &[i64],
) -> Result<SchemaVersion, SchemaError> {
    let latest = known.iter().copied().max().unwrap_or(0);
    // One connection for both reads: a second acquire right after the first
    // release would open another connection of the pool.
    let mut connection = pool.acquire().await?;
    let exists: bool = sqlx::query_scalar("SELECT to_regclass($1) IS NOT NULL")
        .bind(table)
        .fetch_one(&mut *connection)
        .await?;
    if !exists {
        return Ok(SchemaVersion {
            applied: None,
            latest,
        });
    }
    // `table` is a constant of a Linch crate, never a tenant's text.
    let sql = format!("SELECT version FROM {table} WHERE success ORDER BY version");
    let applied: Vec<i64> = sqlx::query_scalar(AssertSqlSafe(sql))
        .fetch_all(&mut *connection)
        .await?;
    if let Some(unknown) = applied.iter().copied().filter(|v| !known.contains(v)).max() {
        return Err(SchemaError::Newer {
            table: table.to_owned(),
            applied: unknown,
            latest,
        });
    }
    Ok(SchemaVersion {
        applied: applied.last().copied(),
        latest,
    })
}
