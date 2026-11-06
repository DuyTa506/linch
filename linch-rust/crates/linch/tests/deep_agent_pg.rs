//! A durable deep agent on Postgres stores: the balanced profile opens,
//! its file memory lives in `linch_files`, and the memory hook reads it in
//! another session of the namespace. Skipped without `LINCH_TEST_PG_DSN`.

use futures::StreamExt;
use linch::events::kinds::HookOutcome;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";
const MODEL: &str = "scripted-model";

/// A fresh schema `p10d_<name>` and a DSN whose connections use it.
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
        let schema = format!("p10d_{name}");
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
        let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");
        Some(Self { admin, schema, dsn })
    }

    /// Every `(namespace, path)` in `linch_files`.
    async fn files(&self) -> Vec<(String, String)> {
        let sql = format!(
            "SELECT namespace, path FROM {}.linch_files ORDER BY path",
            self.schema
        );
        let query = sqlx::query_as(AssertSqlSafe(sql));
        query.fetch_all(&self.admin).await.expect("list the files")
    }

    async fn drop(self) {
        let sql = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the schema");
        self.admin.close().await;
    }
}

fn call(id: &str, tool: &str, input: Value) -> Value {
    json!(TurnBuilder::new(MODEL).tool_call(id, tool, &input).build())
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

/// How each run of the file memory hook ended in a run of `session`.
async fn memory_hooks(agent: &linch::Agent, session: &str) -> Vec<HookOutcome> {
    let mut stream = agent.run("go", RunOptions::new(session));
    let mut outcomes = Vec::new();
    while let Some(item) = stream.next().await {
        if let Event::Hook(hook) = item.unwrap().event
            && hook.policy_id == "linch.deep-agent.file-memory-context"
        {
            outcomes.push(hook.outcome);
        }
    }
    outcomes
}

#[tokio::test]
async fn a_balanced_deep_agent_keeps_its_memory_in_postgres() {
    let Some(db) = TestDb::empty("deep_memory").await else {
        return;
    };
    let index = json!({"path": "/memories/MEMORY.md", "content": "- [Style](style.md)"});
    let turns = vec![
        call("call_1", "write_file", index),
        text("Noted."),
        text("Hello again."),
    ];
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "p10d-tenant",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": db.dsn},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "deep_agent": {},
    }))
    .unwrap();
    let opened = config.open().await.unwrap();
    assert_eq!(opened.agent().spec().max_turns, Some(64));
    let answered = HookOutcome::Answered {
        verdict: "add_context".into(),
    };
    let first = memory_hooks(opened.agent(), "s1").await;
    assert_eq!(
        first,
        [answered.clone(), answered.clone()],
        "one per request"
    );
    let rows = vec![("p10d-tenant".to_owned(), "/memories/MEMORY.md".to_owned())];
    assert_eq!(db.files().await, rows);
    let second = memory_hooks(opened.agent(), "s2").await;
    assert_eq!(second, [answered]);
    opened.close().await;
    db.drop().await;
}
