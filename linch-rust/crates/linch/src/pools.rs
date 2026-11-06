//! [`PgPools`]: one Postgres pool per DSN, shared by the agents opened
//! with it.

use std::collections::HashMap;
use std::fmt;
use std::io;
use std::num::NonZeroU32;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::time::Duration;

use linch_postgres::PgPool;
use tokio::sync::OnceCell;

use crate::error::OpenError;

/// Postgres pools by DSN, for [`AgentConfig::open_with`]: agents opened
/// with the same `PgPools` and the same DSN share one pool, connected and
/// migrated once.
///
/// A pool stays while an [`OpenedAgent`] that uses it is open; closing
/// one agent never closes a pool another uses. Once the last one closes,
/// the next open connects again. DSNs are matched as written, so two
/// spellings of one database get two pools. A pool has the
/// `max_connections` of the open that connected it; the others share it
/// at that size. A server keeps one `PgPools`
/// for all its connections; [`AgentConfig::open`] uses a fresh one, so
/// each agent gets its own pool. `Debug` shows no DSN.
///
/// [`AgentConfig::open_with`]: crate::AgentConfig::open_with
/// [`AgentConfig::open`]: crate::AgentConfig::open
/// [`OpenedAgent`]: crate::OpenedAgent
#[derive(Default)]
pub struct PgPools {
    pools: Mutex<HashMap<String, Weak<Shared>>>,
}

/// One DSN's pool, set once its connect and migrations succeed. Every
/// agent on it holds an `Arc`, which keeps it in its [`PgPools`].
#[derive(Default)]
pub(crate) struct Shared(OnceCell<PgPool>);

impl Shared {
    /// The pool, set once [`PgPools::get`] has returned this slot.
    pub(crate) fn pool(&self) -> Option<&PgPool> {
        self.0.get()
    }
}

impl PgPools {
    /// No pools yet.
    pub fn new() -> Self {
        Self::default()
    }

    /// The pool for `dsn`, connecting it with at most `max_connections`
    /// ([`linch_postgres::MAX_CONNECTIONS`] when `None`) and applying the
    /// migrations if no open agent uses one. A pool in use keeps the size
    /// it was connected with. Concurrent opens of one DSN wait for the
    /// same connect; a failed connect is tried again by the next open.
    pub(crate) async fn get(
        &self,
        dsn: &str,
        max_connections: Option<NonZeroU32>,
    ) -> Result<(Arc<Shared>, PgPool), OpenError> {
        let shared = self.slot(dsn);
        let max_connections = max_connections.unwrap_or(linch_postgres::MAX_CONNECTIONS);
        let pool = shared.0.get_or_try_init(|| connect(dsn, max_connections));
        let pool = pool.await?.clone();
        Ok((shared, pool))
    }

    /// The slot in use for `dsn`, or a new one. Slots no agent holds any
    /// more are dropped first.
    fn slot(&self, dsn: &str) -> Arc<Shared> {
        let mut pools = lock(&self.pools);
        pools.retain(|_, slot| slot.strong_count() > 0);
        if let Some(shared) = pools.get(dsn).and_then(Weak::upgrade) {
            return shared;
        }
        let shared = Arc::new(Shared::default());
        pools.insert(dsn.to_owned(), Arc::downgrade(&shared));
        shared
    }
}

impl fmt::Debug for PgPools {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let pools = lock(&self.pools);
        let live = pools.values().filter(|slot| slot.strong_count() > 0);
        f.debug_struct("PgPools")
            .field("pools", &live.count())
            .finish()
    }
}

/// Longest wait for the migrations: they wait on a database-wide lock, so
/// one stuck migration would otherwise hold every open of every DSN on
/// that database.
const MIGRATE_TIMEOUT: Duration = Duration::from_secs(30);

async fn connect(dsn: &str, max_connections: NonZeroU32) -> Result<PgPool, OpenError> {
    let pool = linch_postgres::connect(dsn, linch_postgres::CONNECT_TIMEOUT, max_connections)
        .await
        .map_err(|error| OpenError::Store(Box::new(error)))?;
    match tokio::time::timeout(MIGRATE_TIMEOUT, linch_postgres::migrate(&pool)).await {
        Ok(migrated) => migrated.map_err(|error| OpenError::Store(Box::new(error)))?,
        Err(_) => {
            let message = format!("the migrations did not finish within {MIGRATE_TIMEOUT:?}");
            let error = io::Error::new(io::ErrorKind::TimedOut, message);
            return Err(OpenError::Store(Box::new(error)));
        }
    }
    Ok(pool)
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code panics while holding this lock.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use sqlx::AssertSqlSafe;
    use sqlx::postgres::PgPoolOptions;

    use super::*;

    #[tokio::test]
    async fn a_dsns_pool_has_the_size_of_the_open_that_connected_it() {
        let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
            eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
            return;
        };
        let schema = format!("pool_size_{}", std::process::id());
        let admin = PgPoolOptions::new().max_connections(1).connect(&dsn).await;
        let admin = admin.expect("connect to the test database");
        let reset = format!("DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}");
        let query = sqlx::raw_sql(AssertSqlSafe(reset));
        query.execute(&admin).await.expect("reset the test schema");
        let separator = if dsn.contains('?') { '&' } else { '?' };
        let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");

        let size = |pool: &PgPool| pool.options().get_max_connections();
        let pools = PgPools::new();
        let (_first, sized) = pools.get(&dsn, NonZeroU32::new(3)).await.unwrap();
        let (_second, shared) = pools.get(&dsn, NonZeroU32::new(7)).await.unwrap();
        assert_eq!((size(&sized), size(&shared)), (3, 3));
        let (_other, default) = PgPools::new().get(&dsn, None).await.unwrap();
        assert_eq!(size(&default), 10);

        let drop = format!("DROP SCHEMA IF EXISTS {schema} CASCADE");
        let query = sqlx::raw_sql(AssertSqlSafe(drop));
        query.execute(&admin).await.expect("drop the test schema");
    }

    #[test]
    fn a_dsn_keeps_one_slot_while_an_agent_holds_it() {
        let pools = PgPools::new();
        let first = pools.slot("postgres://db/a");
        assert!(Arc::ptr_eq(&first, &pools.slot("postgres://db/a")));
        assert!(!Arc::ptr_eq(&first, &pools.slot("postgres://db/b")));
        drop(first);
        let _held = pools.slot("postgres://db/c");
        let dsns: Vec<String> = lock(&pools.pools).keys().cloned().collect();
        assert_eq!(dsns, ["postgres://db/c"]);
        assert_eq!(format!("{pools:?}"), "PgPools { pools: 1 }");
    }
}
