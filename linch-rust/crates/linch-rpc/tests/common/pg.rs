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
    /// An empty schema `p11c_<pid>_<name>`, or `None` (with a note) when the DSN
    /// is unset.
    pub async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p11c_{}_{name}", std::process::id());
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

    /// Drop `table` of the test schema, to make the code that uses it fail.
    pub async fn break_table(&self, table: &str) {
        let sql = format!("DROP TABLE {}.{table} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the table");
    }

    /// Make every insert into `table` of the test schema wait `delay_sql`
    /// first (an SQL expression of seconds, such as `0.2`), so a write the
    /// server has begun is not yet committed. Needs the tables: open an
    /// agent first.
    pub async fn delay_inserts(&self, table: &str, delay_sql: &str) {
        let schema = &self.schema;
        for sql in [
            format!(
                "CREATE FUNCTION {schema}.slow_insert() RETURNS trigger LANGUAGE plpgsql AS \
                 $$ BEGIN PERFORM pg_sleep({delay_sql}); RETURN NEW; END $$"
            ),
            format!(
                "CREATE TRIGGER slow_insert BEFORE INSERT ON {schema}.{table} \
                 FOR EACH ROW EXECUTE FUNCTION {schema}.slow_insert()"
            ),
        ] {
            let query = sqlx::query(AssertSqlSafe(sql));
            query.execute(&self.admin).await.expect("install the delay");
        }
    }

    /// Whether `table` holds a row whose `run_id` is `run_id`.
    pub async fn has_run(&self, table: &str, run_id: &str) -> bool {
        let sql = format!(
            "SELECT count(*) FROM {}.{table} WHERE run_id = $1",
            self.schema
        );
        let query = sqlx::query_scalar::<_, i64>(AssertSqlSafe(sql)).bind(run_id);
        query.fetch_one(&self.admin).await.expect("count the runs") > 0
    }

    /// The `owner` that claimed run `run_id` in `linch_runs`.
    pub async fn run_owner(&self, run_id: &str) -> String {
        let sql = format!(
            "SELECT owner FROM {}.linch_runs WHERE run_id = $1",
            self.schema
        );
        let query = sqlx::query_scalar::<_, String>(AssertSqlSafe(sql)).bind(run_id);
        query.fetch_one(&self.admin).await.expect("read the owner")
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
