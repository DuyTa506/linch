//! The schema version gate: what migrations the database has applied, read
//! without writing, against the ones this binary embeds.

use sqlx::migrate::Migrator;
use sqlx::{AssertSqlSafe, PgConnection, PgPool};
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
    /// Whether the applied migrations are exactly the embedded ones, each
    /// with the embedded checksum: the answer `migrate`'s read-only path
    /// acts on.
    complete: bool,
}

impl SchemaVersion {
    /// Whether every migration this binary embeds is applied, and nothing
    /// else.
    pub fn is_current(&self) -> bool {
        self.complete
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
    check_table(pool, MIGRATIONS_TABLE, &migrator()).await
}

/// [`schema_version`] for any migration table, against the `migrator` its
/// binary embeds (the worker crate checks its own table this way).
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to read.
///     table: The migration table.
///     migrator: Every migration the binary embeds.
///
/// Raises:
///     SchemaError: As [`schema_version`].
pub async fn check_table(
    pool: &PgPool,
    table: &str,
    migrator: &Migrator,
) -> Result<SchemaVersion, SchemaError> {
    let latest = migrator.iter().map(|m| m.version).max().unwrap_or(0);
    let mut connection = pool.acquire().await?;
    let rows = applied(&mut connection, table).await?;
    let succeeded = || {
        rows.iter()
            .filter(|(_, success, _)| *success)
            .map(|(version, ..)| *version)
    };
    if let Some(unknown) = succeeded()
        .filter(|v| !migrator.iter().any(|m| m.version == *v))
        .max()
    {
        return Err(SchemaError::Newer {
            table: table.to_owned(),
            applied: unknown,
            latest,
        });
    }
    Ok(SchemaVersion {
        applied: succeeded().max(),
        latest,
        complete: is_complete(&rows, migrator),
    })
}

/// Whether `table` of the connection's own schema holds exactly the
/// migrations of `migrator`. Any other answer (a missing table, a missing,
/// extra or changed migration, a failed read) is `false`: `migrate` then
/// takes its locked path, which applies what is missing and reports the
/// rest.
pub(crate) async fn is_applied(pool: &PgPool, table: &str, migrator: &Migrator) -> bool {
    let Ok(mut connection) = pool.acquire().await else {
        return false;
    };
    match applied(&mut connection, table).await {
        Ok(rows) => is_complete(&rows, migrator),
        Err(_) => false,
    }
}

/// One row of a migration table: version, success, checksum.
type AppliedRow = (i64, bool, Vec<u8>);

/// The one definition of "the applied set equals the embedded set": the same
/// versions, in order, each successful and with the embedded checksum.
fn is_complete(applied: &[AppliedRow], migrator: &Migrator) -> bool {
    applied.len() == migrator.migrations.len()
        && applied.iter().zip(migrator.iter()).all(|(row, embedded)| {
            let (version, success, checksum) = row;
            *success && *version == embedded.version && checksum[..] == embedded.checksum[..]
        })
}

/// The rows of `table` in the connection's own schema, oldest first, in one
/// query; the table must be the one in `current_schema()`. A table that does
/// not exist (anywhere on the `search_path`) has none.
async fn applied(
    connection: &mut PgConnection,
    table: &str,
) -> Result<Vec<AppliedRow>, sqlx::Error> {
    // `table` is a constant of a Linch crate, never a tenant's text.
    let sql = format!(
        "SELECT m.version, m.success, m.checksum FROM {table} m \
         WHERE '{table}'::regclass = ( \
             SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
             WHERE c.relname = '{table}' AND n.nspname = current_schema()) \
         ORDER BY m.version"
    );
    match sqlx::query_as(AssertSqlSafe(sql))
        .fetch_all(connection)
        .await
    {
        // undefined_table
        Err(error)
            if error
                .as_database_error()
                .is_some_and(|e| e.code().as_deref() == Some("42P01")) =>
        {
            Ok(Vec::new())
        }
        other => other,
    }
}
