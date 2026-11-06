//! Agents opened with one `PgPools` and one DSN share a single Postgres
//! pool, and closing one leaves the pool to the others. Skipped without
//! `LINCH_TEST_PG_DSN`.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, Hooks, OpenedAgent, PgPools, RunOptions, ToolRegistry};
use serde_json::json;
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};
use std::sync::Arc;

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";

/// A fresh schema `p11d_<name>_<pid>` and a DSN that uses it, tagged with
/// that name as its `application_name` so its connections can be counted.
/// `pg_stat_activity` covers the whole cluster, so the process id keeps a
/// concurrent run of this test, on any database, out of the count.
struct TestDb {
    admin: PgPool,
    schema: String,
    dsn: String,
}

impl TestDb {
    async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p11d_{name}_{}", std::process::id());
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

    /// Connections open now with this test's `application_name`.
    async fn connections(&self) -> i64 {
        let sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1";
        let query = sqlx::query_scalar(sql).bind(&self.schema);
        query
            .fetch_one(&self.admin)
            .await
            .expect("count connections")
    }

    async fn drop(self) {
        let sql = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query
            .execute(&self.admin)
            .await
            .expect("drop the test schema");
    }
}

async fn open(dsn: &str, pools: &PgPools, text: &str) -> OpenedAgent {
    let turns = json!([TurnBuilder::new("scripted-model").text(text).build()]);
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "p11d",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": dsn},
    }))
    .unwrap();
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    config.open_with(tools, hooks, pools).await.unwrap()
}

#[tokio::test]
async fn agents_on_one_dsn_share_a_pool_that_outlives_each_close() {
    let Some(db) = TestDb::empty("pools_share").await else {
        return;
    };
    let pools = PgPools::new();
    let first = open(&db.dsn, &pools, "one").await;
    let second = open(&db.dsn, &pools, "two").await;
    assert_eq!(db.connections().await, 1);
    // Another `PgPools` has its own pool, so the count does tell them apart.
    let apart = open(&db.dsn, &PgPools::new(), "three").await;
    assert_eq!(db.connections().await, 2);
    apart.close().await;

    first.close().await;
    let mut stream = second.agent().run("go", RunOptions::new("s1"));
    let mut last = None;
    while let Some(item) = stream.next().await {
        last = Some(item.expect("the shared pool is still open").event);
    }
    match last {
        Some(Event::Result(result)) => assert_eq!(result.final_text.as_deref(), Some("two")),
        other => panic!("expected a result, got {other:?}"),
    }
    second.close().await;
    db.drop().await;
}
