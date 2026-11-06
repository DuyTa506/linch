//! Postgres run, session and virtual file stores for the Linch agent kernel.
//!
//! [`PgRunStore`] and [`PgSessionStore`] implement linch-core's
//! [`RunStore`](linch_core::RunStore) and
//! [`SessionStore`](linch_core::SessionStore) and pass its conformance suite;
//! [`PgFileBackend`] implements linch-tools'
//! [`FileBackend`](linch_tools::vfs::FileBackend) and passes its suite. All
//! take a [`PgPool`]; tables are created by [`migrate`], which the host runs
//! once at startup:
//!
//! ```no_run
//! # async fn start(dsn: &str) -> Result<(), Box<dyn std::error::Error>> {
//! let pool = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT).await?;
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
use std::time::Duration;

use sqlx::migrate::{Migration, MigrationType, Migrator};
use sqlx::{Postgres, SqlSafeStr, Transaction};

mod codec;
mod files;
mod run;
mod session;

pub use codec::CodecError;
pub use files::PgFileBackend;
pub use run::PgRunStore;
pub use session::PgSessionStore;
pub use sqlx::PgPool;
pub use sqlx::migrate::MigrateError;

/// Table that records which migrations have been applied.
///
/// It is not sqlx's default `_sqlx_migrations`, so a host that runs its own
/// sqlx migrations in the same schema does not collide with these.
pub const MIGRATIONS_TABLE: &str = "linch_migrations";

/// How long [`connect`] waits for the first connection by default.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Connect a pool, failing after `timeout` when no first connection can
/// be opened.
///
/// sqlx's own `PgPool::connect` retries a refused connection until its
/// 30 s acquire timeout, so an unreachable database would hold the caller
/// that long. Only this first connect is bounded; later waits for a pooled
/// connection keep sqlx's defaults.
///
/// Args:
///     dsn: Connection string, for example `postgres://user:pass@host/db`.
///     timeout: Longest wait for the first connection.
///
/// Returns:
///     A pool holding one open connection.
///
/// Raises:
///     sqlx::Error: `PoolTimedOut` when no connection opened in time,
///         `Configuration` for a malformed DSN, or the server's refusal
///         (bad password, unknown database). No message holds the DSN.
pub async fn connect(dsn: &str, timeout: Duration) -> Result<PgPool, sqlx::Error> {
    match tokio::time::timeout(timeout, PgPool::connect(dsn)).await {
        Ok(connected) => connected,
        Err(_) => Err(sqlx::Error::PoolTimedOut),
    }
}

/// Begin a READ COMMITTED transaction whatever the server or role default.
///
/// Every write locks its parent row with `FOR UPDATE` and must read the row
/// again after waiting on another writer; REPEATABLE READ or SERIALIZABLE
/// would fail that wait with a serialization error instead.
pub(crate) async fn begin(pool: &PgPool) -> Result<Transaction<'static, Postgres>, sqlx::Error> {
    pool.begin_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        .await
}

/// The migrations embedded in this crate, newest last.
fn migrator() -> Migrator {
    let migrations = vec![
        Migration::new(
            1,
            Cow::Borrowed("stores"),
            MigrationType::Simple,
            include_str!("../migrations/0001_stores.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            2,
            Cow::Borrowed("one live run per session"),
            MigrationType::Simple,
            include_str!("../migrations/0002_one_live_run_per_session.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            3,
            Cow::Borrowed("files"),
            MigrationType::Simple,
            include_str!("../migrations/0003_files.sql").into_sql_str(),
            false,
        ),
    ];
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
