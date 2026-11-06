//! On Postgres stores the virtual files are the namespace's rows in
//! `linch_files`, shared by its sessions. Skipped without
//! `LINCH_TEST_PG_DSN`.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";
const MODEL: &str = "scripted-model";

/// A fresh schema `p10a_<name>` and a DSN whose connections use it.
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
        let schema = format!("p10a_{name}");
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

/// The result text of the run's one tool call.
async fn tool_result(agent: &linch::Agent, session: &str) -> String {
    let mut stream = agent.run("go", RunOptions::new(session));
    let mut result = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            let value = serde_json::to_value(&end.result).unwrap();
            result = value["content"][0]["text"].as_str().map(str::to_owned);
        }
    }
    result.expect("the call ended")
}

#[tokio::test]
async fn the_sessions_of_a_namespace_share_its_postgres_files() {
    let Some(db) = TestDb::empty("vfs_files").await else {
        return;
    };
    let turns = vec![
        call(
            "call_1",
            "write_file",
            json!({"path": "/notes.md", "content": "alpha"}),
        ),
        text("Wrote."),
        call("call_2", "read_file", json!({"path": "/notes.md"})),
        text("Read."),
    ];
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "p10a-tenant",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "stores": {"type": "postgres", "dsn": db.dsn},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
        "filesystem": {},
    }))
    .unwrap();
    let opened = config.open().await.unwrap();
    let wrote = tool_result(opened.agent(), "s1").await;
    assert!(wrote.contains("/notes.md"), "{wrote}");
    let read = tool_result(opened.agent(), "s2").await;
    assert!(read.contains("alpha"), "{read}");
    let rows = vec![("p10a-tenant".to_owned(), "/notes.md".to_owned())];
    assert_eq!(db.files().await, rows);
    opened.close().await;
    db.drop().await;
}
