//! A fresh Postgres schema per test.
//!
//! Tests read the database from `LINCH_TEST_PG_DSN` and skip without it.
//! Each test gets its own schema (`p10w_<name>`, reset at start and dropped
//! at the end) through the pool's `search_path`, so tests run in parallel.

use std::future::Future;
use std::str::FromStr;
use std::time::{Duration, Instant};

use linch_ext::PgPool;
use sqlx::AssertSqlSafe;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";

pub struct TestDb {
    /// Pool without a search path, for schema setup.
    admin: PgPool,
    options: PgConnectOptions,
    schema: String,
    pools: Vec<PgPool>,
}

impl TestDb {
    /// An empty schema, or `None` (with a note) when the DSN is unset.
    pub async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p10w_{name}");
        let admin = PgPoolOptions::new()
            .max_connections(2)
            .connect(&dsn)
            .await
            .expect("connect to the test database");
        for sql in [
            format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
            format!("CREATE SCHEMA {schema}"),
        ] {
            let query = sqlx::query(AssertSqlSafe(sql));
            query.execute(&admin).await.expect("reset the test schema");
        }
        let options = PgConnectOptions::from_str(&dsn)
            .expect("a valid DSN")
            .options([("search_path", schema.as_str())]);
        Some(Self {
            admin,
            options,
            schema,
            pools: Vec::new(),
        })
    }

    /// A schema with linch-ext's tables.
    pub async fn migrated(name: &str) -> Option<Self> {
        let mut db = Self::empty(name).await?;
        let pool = db.pool().await;
        linch_ext::migrate(&pool).await.expect("migrate");
        Some(db)
    }

    /// A new pool on the test schema: one per simulated process.
    pub async fn pool(&mut self) -> PgPool {
        self.pool_with(self.options.clone()).await
    }

    /// A pool like [`Self::pool`] whose transactions default to REPEATABLE
    /// READ, as a host's role may set.
    pub async fn repeatable_read(&mut self) -> PgPool {
        let isolation = ("default_transaction_isolation", "repeatable read");
        self.pool_with(self.options.clone().options([isolation]))
            .await
    }

    async fn pool_with(&mut self, options: PgConnectOptions) -> PgPool {
        let pool = PgPoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .expect("connect to the test schema");
        self.pools.push(pool.clone());
        pool
    }

    /// Run `write` while another transaction holds `update`, and commit
    /// that transaction once `write` waits on its row lock.
    pub async fn behind_an_update<T>(&self, update: &str, write: impl Future<Output = T>) -> T {
        let mut holder = self.admin.begin().await.unwrap();
        let sql = format!("SET LOCAL search_path TO {}", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&mut *holder).await.unwrap();
        let query = sqlx::query(AssertSqlSafe(update.to_owned()));
        let updated = query.execute(&mut *holder).await.unwrap();
        assert_eq!(updated.rows_affected(), 1, "{update}");
        let release = async {
            let deadline = Instant::now() + Duration::from_secs(10);
            // pg_locks, unlike pg_stat_activity, is not a snapshot per
            // transaction.
            while sqlx::query_scalar::<_, i32>(
                "SELECT pid FROM pg_locks \
                 WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))",
            )
            .fetch_optional(&mut *holder)
            .await
            .unwrap()
            .is_none()
            {
                assert!(Instant::now() < deadline, "the write never waited");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            holder.commit().await.unwrap();
        };
        let (result, ()) = tokio::join!(write, release);
        result
    }

    /// Close the pools and drop the schema.
    pub async fn finish(self) {
        for pool in self.pools {
            pool.close().await;
        }
        let sql = format!("DROP SCHEMA {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the schema");
        self.admin.close().await;
    }
}
