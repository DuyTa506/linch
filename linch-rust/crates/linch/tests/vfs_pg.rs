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

/// Where the offload of `Read` of `file` under `root` goes: the path names a
/// digest of the text the tool returns, so run the tool to get it.
async fn read_offload_path(root: &std::path::Path, file: &str) -> String {
    let fs = std::sync::Arc::new(linch::tools::LocalFs::new(root).unwrap());
    let read = linch::tools::Read::new(fs, std::sync::Arc::new(linch::tools::ReadTracker::new()));
    let ctx = linch::ToolContext::new(
        "s1".into(),
        "r1".into(),
        "call_1",
        None,
        tokio_util::sync::CancellationToken::new(),
        None,
    );
    let input = json!({"file_path": file}).as_object().cloned().unwrap();
    let output = linch::Tool::execute(&read, input, ctx).await.unwrap();
    let text = output.value.as_str().unwrap().to_owned();
    linch::tools::vfs::offload_path("/offload", "s1", "Read", "call_1", &text)
}

/// A fresh schema `p10a_<pid>_<name>` and a DSN whose connections use it.
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
        let schema = format!("p10a_{}_{name}", std::process::id());
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

/// One minified line, offloaded into Postgres and paged back by byte.
#[tokio::test]
async fn a_minified_line_is_offloaded_into_postgres_and_paged_back_by_byte() {
    let Some(db) = TestDb::empty("vfs_one_line").await else {
        return;
    };
    let workspace = tempfile::tempdir().unwrap();
    let line = format!("{}MARK{}", "a".repeat(20_000), "b".repeat(280_000));
    std::fs::write(workspace.path().join("min.json"), &line).unwrap();
    let open = |turns: Vec<Value>| {
        let config: AgentConfig = serde_json::from_value(json!({
            "namespace": "p10a-tenant",
            "spec": {"model": MODEL},
            "provider": {"id": "scripted", "turns": turns},
            "stores": {"type": "postgres", "dsn": db.dsn},
            "permissions": {"mode": "accept_edits", "project_root": "/"},
            "workspace": workspace.path(),
            "filesystem": {"offload_threshold_tokens": 100},
        }))
        .unwrap();
        config.open()
    };
    let path = read_offload_path(workspace.path(), "min.json").await;
    let turns = vec![
        call("call_1", "Read", json!({"file_path": "min.json"})),
        text("Read."),
    ];
    let opened = open(turns).await.unwrap();
    let pointer = tool_result(opened.agent(), "s1").await;
    assert!(
        pointer.len() < 4_000,
        "the pointer is small: {}",
        pointer.len()
    );
    assert!(!pointer.contains("MARK"), "{pointer}");
    opened.close().await;

    let first = json!({"path": path, "offset": 1, "limit": 1});
    let second = json!({"path": path, "offset": 1, "limit": 1, "byte_offset": 16384});
    for (n, (read, wanted)) in [(first, "continues at byte 16384;"), (second, "MARK")]
        .into_iter()
        .enumerate()
    {
        let turns = vec![call("call_2", "read_file", read), text("Read.")];
        let opened = open(turns).await.unwrap();
        let page = tool_result(opened.agent(), &format!("s{}", n + 2)).await;
        assert!(
            page.contains(wanted),
            "page {n}: {}",
            &page[page.len() - 200..]
        );
        assert!(page.len() < 17_000, "page {n} holds {} bytes", page.len());
        opened.close().await;
    }
    db.drop().await;
}

#[tokio::test]
async fn an_offloaded_result_outlives_the_agent_and_stays_out_of_the_workspace() {
    let Some(db) = TestDb::empty("vfs_offload").await else {
        return;
    };
    let workspace = tempfile::tempdir().unwrap();
    let body: Vec<String> = (1..=200).map(|n| format!("line {n}")).collect();
    std::fs::write(workspace.path().join("big.txt"), body.join("\n")).unwrap();
    let open = |turns: Vec<Value>| {
        let config: AgentConfig = serde_json::from_value(json!({
            "namespace": "p10a-tenant",
            "spec": {"model": MODEL},
            "provider": {"id": "scripted", "turns": turns},
            "stores": {"type": "postgres", "dsn": db.dsn},
            "permissions": {"mode": "accept_edits", "project_root": "/"},
            "workspace": workspace.path(),
            "filesystem": {"offload_threshold_tokens": 10},
        }))
        .unwrap();
        config.open()
    };
    let path = read_offload_path(workspace.path(), "big.txt").await;
    let turns = vec![
        call("call_1", "Read", json!({"file_path": "big.txt"})),
        text("Read."),
    ];
    let opened = open(turns).await.unwrap();
    let pointer = tool_result(opened.agent(), "s1").await;
    assert!(pointer.contains(&path), "{pointer}");
    opened.close().await;
    assert_eq!(db.files().await, [("p10a-tenant".to_owned(), path.clone())]);
    let names: Vec<_> = std::fs::read_dir(workspace.path()).unwrap().collect();
    assert_eq!(
        names.len(),
        1,
        "only big.txt is in the workspace: {names:?}"
    );

    // A second agent, as another process would be, reads it back.
    let turns = vec![
        call(
            "call_2",
            "read_file",
            json!({"path": path, "offset": 150, "limit": 1}),
        ),
        text("Read."),
    ];
    let opened = open(turns).await.unwrap();
    let back = tool_result(opened.agent(), "s2").await;
    assert!(back.contains("line 150"), "{back}");
    opened.close().await;
    db.drop().await;
}
