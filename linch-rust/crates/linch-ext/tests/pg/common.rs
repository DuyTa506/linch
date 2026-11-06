//! A fresh Postgres schema per test.
//!
//! Tests read the database from `LINCH_TEST_PG_DSN` and skip without it.
//! Each test gets its own schema (`p10w_<name>`, reset at start and dropped
//! at the end) through the pool's `search_path`, so tests run in parallel.

use std::str::FromStr;

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
        let pool = PgPoolOptions::new()
            .max_connections(4)
            .connect_with(self.options.clone())
            .await
            .expect("connect to the test schema");
        self.pools.push(pool.clone());
        pool
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
