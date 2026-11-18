//! [`migrate`] and [`check`]: the schema as a deploy step. A host runs
//! `migrate` once before it starts servers on a new release; every open and
//! worker start then finds the schema current.

use std::num::NonZeroU32;

use linch_postgres::{SchemaError, SchemaVersion};

/// Connections of the pool a deploy step uses; the migration lock takes one
/// of its own besides.
const POOL_SIZE: NonZeroU32 = NonZeroU32::new(2).unwrap();

/// The migrations a database holds, against what this binary embeds, for
/// the store tables and the worker's queue tables.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchemaStatus {
    /// Runs, sessions, files, mailbox, memories and the tool cache.
    pub store: SchemaVersion,
    /// The worker's queue and schedule tables.
    pub worker: SchemaVersion,
}

impl SchemaStatus {
    /// Whether every migration this binary embeds is applied.
    pub fn is_current(&self) -> bool {
        self.store.is_current() && self.worker.is_current()
    }
}

/// Apply every migration this binary embeds, the store's and the worker's,
/// in the database and `search_path` schema `dsn` names.
///
/// Safe to run on every deploy and from several hosts at once: migrations
/// that are applied are skipped, and runners take one advisory lock per
/// schema. It refuses a database a newer release migrated, before it
/// writes anything.
///
/// Args:
///     dsn: The connection string the agents' `stores.dsn` uses.
///
/// Returns:
///     The versions now applied; both are current.
///
/// Raises:
///     SchemaError: `Newer` when the database holds a migration this binary
///         does not embed; `Database` or `Migrate` when it cannot connect or
///         a migration fails. No message holds the DSN.
pub async fn migrate(dsn: &str) -> Result<SchemaStatus, SchemaError> {
    let pool = connect(dsn).await?;
    check_pool(&pool).await?;
    linch_postgres::migrate(&pool).await?;
    linch_worker::migrate(&pool).await?;
    let status = check_pool(&pool).await?;
    pool.close().await;
    Ok(status)
}

/// What `dsn`'s database has applied against this binary, without writing:
/// a database never migrated shows `applied: None`, and one behind this
/// binary shows an applied version under the latest.
///
/// Args:
///     dsn: The connection string the agents' `stores.dsn` uses.
///
/// Raises:
///     SchemaError: `Newer` when the database holds a migration this binary
///         does not embed; `Database` when it cannot be read.
pub async fn check(dsn: &str) -> Result<SchemaStatus, SchemaError> {
    let pool = connect(dsn).await?;
    let status = check_pool(&pool).await?;
    pool.close().await;
    Ok(status)
}

async fn connect(dsn: &str) -> Result<linch_postgres::PgPool, SchemaError> {
    let connected = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT, POOL_SIZE).await;
    Ok(connected?)
}

async fn check_pool(pool: &linch_postgres::PgPool) -> Result<SchemaStatus, SchemaError> {
    Ok(SchemaStatus {
        store: linch_postgres::schema_version(pool).await?,
        worker: linch_worker::schema_version(pool).await?,
    })
}
