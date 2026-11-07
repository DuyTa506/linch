//! The Postgres migrations of linch-ext's stores.

use std::borrow::Cow;

use sqlx::PgPool;
use sqlx::SqlSafeStr;
use sqlx::migrate::{MigrateError, Migration, MigrationType, Migrator};

/// Table that records which linch-ext migrations have been applied.
///
/// It is neither sqlx's `_sqlx_migrations` nor another Linch crate's
/// table (`linch_migrations`, `linch_worker_migrations`), so migration
/// versions never collide.
pub const MIGRATIONS_TABLE: &str = "linch_ext_migrations";

fn migrator() -> Migrator {
    let migrations = vec![Migration::new(
        1,
        Cow::Borrowed("workflow"),
        MigrationType::Simple,
        include_str!("../migrations/0001_workflow.sql").into_sql_str(),
        false,
    )];
    Migrator {
        table_name: Cow::Borrowed(MIGRATIONS_TABLE),
        ..Migrator::with_migrations(migrations)
    }
}

/// Create or upgrade linch-ext's tables (the workflow journal) in the
/// pool's database.
///
/// Safe to call on every start: applied migrations are skipped, and
/// concurrent callers serialize on a Postgres advisory lock. Tables are
/// unqualified and prefixed `linch_ext_`, so the connection's
/// `search_path` picks the schema.
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to migrate.
///
/// Raises:
///     MigrateError: If the database is unreachable, a migration fails, or
///         an applied migration differs from the embedded one.
pub async fn migrate(pool: &PgPool) -> Result<(), MigrateError> {
    migrator().run(pool).await
}
