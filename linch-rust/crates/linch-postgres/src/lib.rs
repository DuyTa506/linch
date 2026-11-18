//! Postgres run, session and virtual file stores for the Linch agent kernel.
//!
//! [`PgRunStore`] and [`PgSessionStore`] implement linch-core's
//! [`RunStore`](linch_core::RunStore) and
//! [`SessionStore`](linch_core::SessionStore) and pass its conformance suite;
//! [`PgSessionStore`] also implements [`TaskStore`](linch_core::TaskStore)
//! and passes its suite;
//! [`PgFileBackend`] implements linch-core's
//! [`FileBackend`](linch_core::vfs::FileBackend) and passes its suite;
//! [`PgMailbox`] implements linch-core's [`Mailbox`](linch_core::Mailbox)
//! and passes its suite; [`PgMemoryStore`] implements
//! [`MemoryStore`](linch_core::MemoryStore) and passes its suite. All take a [`PgPool`]; tables are created by [`migrate`], which the host runs
//! once at startup:
//!
//! ```no_run
//! # async fn start(dsn: &str) -> Result<(), Box<dyn std::error::Error>> {
//! use linch_postgres::{CONNECT_TIMEOUT, default_max_connections};
//!
//! let pool = linch_postgres::connect(dsn, CONNECT_TIMEOUT, default_max_connections()).await?;
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
use std::io;
use std::num::NonZeroU32;
use std::str::FromStr;
use std::time::Duration;

use sqlx::migrate::{Migration, MigrationType, Migrator};
use sqlx::postgres::{PgConnectOptions, PgConnection, PgPoolOptions};
use sqlx::{AssertSqlSafe, Connection, Postgres, SqlSafeStr, Transaction};

mod codec;
mod files;
mod lease_line;
mod mailbox;
mod memory;
mod pool_size;
mod run;
mod session;
mod tasks;
mod tool_cache;

pub use codec::CodecError;
pub use files::PgFileBackend;
pub use lease_line::LeaseLine;
pub use mailbox::{DEFAULT_LEASE, PgMailbox};
pub use memory::{DEFAULT_STATEMENT_TIMEOUT, PgMemoryStore};
pub use pool_size::{MAX_DEFAULT_CONNECTIONS, MIN_DEFAULT_CONNECTIONS, default_max_connections};
pub use run::{DEFAULT_RUN_LEASE_TTL, PgRunStore};
pub use session::PgSessionStore;
pub use sqlx::PgPool;
pub use sqlx::migrate::MigrateError;
pub use tool_cache::PgToolCache;

/// Table that records which migrations have been applied.
///
/// It is not sqlx's default `_sqlx_migrations`, so a host that runs its own
/// sqlx migrations in the same schema does not collide with these.
pub const MIGRATIONS_TABLE: &str = "linch_migrations";

/// How long [`connect`] waits for the first connection by default.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// How long a connection sits idle in a [`connect`] pool before the pool
/// pings it on its way out.
pub const IDLE_PING_AFTER: Duration = Duration::from_secs(1);

/// Connect a pool, failing after `timeout` when no first connection can
/// be opened.
///
/// sqlx's own `PgPool::connect` retries a refused connection until its
/// 30 s acquire timeout, so an unreachable database would hold the caller
/// that long. Only this first connect is bounded; later waits for a pooled
/// connection keep sqlx's defaults.
///
/// The pool pings a connection before handing it out only when it sat
/// idle for [`IDLE_PING_AFTER`] or longer, and replaces it when the ping
/// fails. A connection the server closed during such a wait (a restart, a
/// failover, `idle_session_timeout`, a proxy's idle kill) is replaced
/// instead of failing the next write, so the receipt write after a long
/// model call lands, and a resume does not pay for the call again. Writes
/// that follow each other closely (a run's barrier writes) skip the ping:
/// a run commit or session append stays one round trip. sqlx's own ping
/// before every acquire (`test_before_acquire`) would make each of them
/// two.
///
/// Args:
///     dsn: Connection string, for example `postgres://user:pass@host/db`.
///     timeout: Longest wait for the first connection.
///     max_connections: The most connections the pool opens at once
///         ([`default_max_connections`] unless the host needs more).
///
/// Returns:
///     A pool holding one open connection.
///
/// Raises:
///     sqlx::Error: `Io` of kind `TimedOut` when no connection opened in
///         time, naming the host and port (or socket) it tried;
///         `Configuration` for a malformed DSN; or the server's refusal
///         (bad password, unknown database). No message holds the DSN.
pub async fn connect(
    dsn: &str,
    timeout: Duration,
    max_connections: NonZeroU32,
) -> Result<PgPool, sqlx::Error> {
    let options = PgConnectOptions::from_str(dsn)?;
    let target = match options.get_socket() {
        Some(socket) => format!("{} (port {})", socket.display(), options.get_port()),
        None if options.get_host().contains(':') => {
            format!("[{}]:{}", options.get_host(), options.get_port())
        }
        None => format!("{}:{}", options.get_host(), options.get_port()),
    };
    let pool = PgPoolOptions::new()
        .test_before_acquire(false)
        .before_acquire(|connection, meta| {
            Box::pin(async move {
                // A failed ping closes this connection; the pool hands out
                // another one, or opens a new one.
                if meta.idle_for >= IDLE_PING_AFTER {
                    connection.ping().await?;
                }
                Ok(true)
            })
        })
        .max_connections(max_connections.get());
    match tokio::time::timeout(timeout, pool.connect_with(options)).await {
        Ok(connected) => connected,
        Err(_) => {
            let message = format!("cannot reach Postgres at {target} within {timeout:?}");
            Err(sqlx::Error::Io(io::Error::new(
                io::ErrorKind::TimedOut,
                message,
            )))
        }
    }
}

/// Begin a READ COMMITTED transaction whatever the server or role default.
///
/// A write of several statements (run create and claim, file edit) locks
/// its parent row with `FOR UPDATE` and must read the row again after
/// waiting on another writer; REPEATABLE READ or SERIALIZABLE would fail
/// that wait with a serialization error instead.
pub(crate) async fn begin(pool: &PgPool) -> Result<Transaction<'static, Postgres>, sqlx::Error> {
    pool.begin_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        .await
}

/// Most times a single-statement write (run commit, session append) runs
/// when its row keeps changing under it.
pub(crate) const WRITE_TRIES: u32 = 3;

/// Whether a single-statement write failed with a serialization error.
///
/// Such a write runs in autocommit, at the pool's default isolation. Above
/// READ COMMITTED, a row changed after the statement's snapshot fails it
/// with this error; it wrote nothing, and runs again on a fresh snapshot.
/// Under READ COMMITTED it never happens.
pub(crate) fn serialization_failure(error: &sqlx::Error) -> bool {
    let code = error.as_database_error().and_then(|error| error.code());
    code.as_deref() == Some("40001")
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
        Migration::new(
            4,
            Cow::Borrowed("tasks"),
            MigrationType::Simple,
            include_str!("../migrations/0004_tasks.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            5,
            Cow::Borrowed("mailbox"),
            MigrationType::Simple,
            include_str!("../migrations/0005_mailbox.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            6,
            Cow::Borrowed("run leases"),
            MigrationType::Simple,
            include_str!("../migrations/0006_run_leases.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            7,
            Cow::Borrowed("run principal"),
            MigrationType::Simple,
            include_str!("../migrations/0007_run_principal.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            8,
            Cow::Borrowed("tool cache"),
            MigrationType::Simple,
            include_str!("../migrations/0008_tool_cache.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            9,
            Cow::Borrowed("memories"),
            MigrationType::Simple,
            include_str!("../migrations/0009_memories.sql").into_sql_str(),
            false,
        ),
        Migration::new(
            10,
            Cow::Borrowed("retention"),
            MigrationType::Simple,
            include_str!("../migrations/0010_retention.sql").into_sql_str(),
            false,
        ),
    ];
    Migrator {
        table_name: Cow::Borrowed(MIGRATIONS_TABLE),
        // `migrate` takes its own per-schema lock; sqlx's is database-wide.
        locking: false,
        ..Migrator::with_migrations(migrations)
    }
}

/// Create or upgrade the store tables in the pool's database.
///
/// Safe to call on every start: applied migrations are skipped, and
/// concurrent callers serialize on a Postgres advisory lock scoped to the
/// database and `search_path` schema, so schemas migrate independently.
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
    let migrator = migrator();
    // Starting a server on a schema that is already migrated only reads: it
    // takes no lock, so it does not wait behind a migration that is running
    // (or hold others up for the milliseconds of a lock and a connection).
    if all_applied(pool, &migrator).await {
        return Ok(());
    }
    // The lock lives on a connection of its own, outside the pool, so a
    // one-connection pool can still migrate; closing it (or dropping this
    // future) releases the lock.
    let mut lock = PgConnection::connect_with(&pool.connect_options()).await?;
    sqlx::query(MIGRATE_LOCK).execute(&mut lock).await?;
    let migrated = migrator.run(pool).await;
    let _ = lock.close().await;
    migrated
}

/// Whether the migrations table of the connection's own schema holds exactly
/// the embedded migrations, each applied with the embedded checksum. Any
/// other answer (a missing table, a missing, extra or changed migration, a
/// failed read) is `false`, and [`migrate`] takes its locked path, which
/// applies what is missing and reports the rest as it always did.
async fn all_applied(pool: &PgPool, migrator: &Migrator) -> bool {
    let applied: Result<Vec<(i64, bool, Vec<u8>)>, _> =
        sqlx::query_as(applied_sql()).fetch_all(pool).await;
    let Ok(applied) = applied else {
        return false;
    };
    applied.len() == migrator.migrations.len()
        && applied.iter().zip(migrator.iter()).all(|(row, embedded)| {
            let (version, success, checksum) = row;
            *success && *version == embedded.version && checksum[..] == embedded.checksum[..]
        })
}

/// The applied migrations of the connection's own schema, oldest first:
/// the table must be the one in `current_schema()`.
fn applied_sql() -> impl SqlSafeStr {
    AssertSqlSafe(format!(
        "SELECT m.version, m.success, m.checksum FROM {MIGRATIONS_TABLE} m \
         WHERE '{MIGRATIONS_TABLE}'::regclass = ( \
             SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
             WHERE c.relname = '{MIGRATIONS_TABLE}' AND n.nspname = current_schema()) \
         ORDER BY m.version"
    ))
}

const MIGRATE_LOCK: &str = "SELECT pg_advisory_lock(hashtextextended(\
    'linch_migrate:' || current_database() || ':' || coalesce(current_schema(), ''), 0))";
