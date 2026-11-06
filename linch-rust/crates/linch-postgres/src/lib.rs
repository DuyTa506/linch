//! Postgres run and session stores for the Linch agent kernel.
//!
//! [`PgRunStore`] and [`PgSessionStore`] implement linch-core's
//! [`RunStore`](linch_core::RunStore) and
//! [`SessionStore`](linch_core::SessionStore) and pass its conformance suite.
//! Both take a [`PgPool`]; tables are created by [`migrate`], which the host
//! runs once at startup:
//!
//! ```no_run
//! # async fn start(dsn: &str) -> Result<(), Box<dyn std::error::Error>> {
//! let pool = linch_postgres::PgPool::connect(dsn).await?;
//! linch_postgres::migrate(&pool).await?;
//! let runs = linch_postgres::PgRunStore::new(pool.clone());
//! let sessions = linch_postgres::PgSessionStore::new(pool);
//! # Ok(()) }
//! ```
//!
//! Tables are unqualified and prefixed `linch_`, so a host picks the schema
//! with the connection's `search_path`. Every query is built at run time, so
//! building this crate never needs a database.

#![forbid(unsafe_code)]

use std::borrow::Cow;

use sqlx::SqlSafeStr;
use sqlx::migrate::{Migration, MigrationType, Migrator};

mod codec;
mod run;
mod session;

pub use codec::CodecError;
pub use run::PgRunStore;
pub use session::PgSessionStore;
pub use sqlx::PgPool;
pub use sqlx::migrate::MigrateError;

/// Table that records which migrations have been applied.
///
/// It is not sqlx's default `_sqlx_migrations`, so a host that runs its own
/// sqlx migrations in the same schema does not collide with these.
pub const MIGRATIONS_TABLE: &str = "linch_migrations";

/// The migrations embedded in this crate, newest last.
fn migrator() -> Migrator {
    let migrations = vec![Migration::new(
        1,
        Cow::Borrowed("stores"),
        MigrationType::Simple,
        include_str!("../migrations/0001_stores.sql").into_sql_str(),
        false,
    )];
    Migrator {
        table_name: Cow::Borrowed(MIGRATIONS_TABLE),
        ..Migrator::with_migrations(migrations)
    }
}

/// Create or upgrade the store tables in the pool's database.
///
/// Safe to call on every start: applied migrations are skipped, and
/// concurrent callers serialize on a Postgres advisory lock.
///
/// Args:
///     pool: Pool for the database (and `search_path` schema) to migrate.
///
/// Returns:
///     Nothing once every migration is applied.
///
/// Raises:
///     MigrateError: If the database is unreachable, a migration fails, or
///         an applied migration differs from the embedded one.
pub async fn migrate(pool: &PgPool) -> Result<(), MigrateError> {
    migrator().run(pool).await
}
