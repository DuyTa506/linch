//! A fresh Postgres schema for one test, reached through a DSN whose
//! `search_path` is that schema. Tests read the database from
//! `LINCH_TEST_PG_DSN` and skip without it.

use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";

pub struct TestDb {
    admin: PgPool,
    schema: String,
    /// A DSN for `agent.open` whose connections use the test schema.
    pub dsn: String,
}

impl TestDb {
    /// An empty schema `p11c_<name>`, or `None` (with a note) when the DSN
    /// is unset.
    pub async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p11c_{name}");
        let admin = PgPoolOptions::new()
            .max_connections(1)
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
        let separator = if dsn.contains('?') { '&' } else { '?' };
        let dsn = format!(
            "{dsn}{separator}options=-c%20search_path%3D{schema}&application_name={schema}"
        );
        Some(Self { admin, schema, dsn })
    }

    /// Connections open now through [`Self::dsn`] (its `application_name`).
    pub async fn connections(&self) -> i64 {
        let sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1";
        let query = sqlx::query_scalar(sql).bind(&self.schema);
        query
            .fetch_one(&self.admin)
            .await
            .expect("count connections")
    }

    /// Drop the schema.
    pub async fn drop(self) {
        let sql = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query
            .execute(&self.admin)
            .await
            .expect("drop the test schema");
    }
}
