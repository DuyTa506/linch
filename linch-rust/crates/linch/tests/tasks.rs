//! The session task tools an `AgentConfig` turns on: off by default, run
//! without approval in every mode, kept per session in the session store,
//! and on Postgres stores durable across agents (skipped without
//! `LINCH_TEST_PG_DSN`).

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, OpenedAgent, RunOptions};
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const MODEL: &str = "scripted-model";
const TASKS: [&str; 4] = ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate"];

fn call(tool: &str, input: Value) -> Value {
    json!(
        TurnBuilder::new(MODEL)
            .tool_call("call_1", tool, &input)
            .build()
    )
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

/// A scripted config in the default permission mode (write tools ask),
/// with `extra` fields on top.
fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

/// What one run showed: its tools, each tool result's text, and the final
/// text (`None` when the run did not finish with a result).
struct Ran {
    tools: Vec<String>,
    results: Vec<String>,
    final_text: Option<String>,
}

async fn run(opened: &OpenedAgent, session: &str) -> Ran {
    let mut stream = opened.agent().run("go", RunOptions::new(session));
    let mut ran = Ran {
        tools: Vec::new(),
        results: Vec::new(),
        final_text: None,
    };
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::Init(init) => ran.tools = init.tools,
            Event::ToolCallEnd(end) => {
                let result = serde_json::to_value(&end.result).unwrap();
                let text = result["content"][0]["text"].as_str().unwrap_or_default();
                ran.results.push(text.to_owned());
            }
            Event::Result(result) => ran.final_text = result.final_text,
            _ => {}
        }
    }
    ran
}

#[tokio::test]
async fn the_tools_are_off_unless_the_config_turns_them_on() {
    let off = config(vec![text("hi")], json!({}));
    assert!(!off.tasks);
    let echoed = serde_json::to_value(&off).unwrap();
    assert!(echoed.get("tasks").is_none(), "{echoed}");
    let opened = off.open().await.unwrap();
    assert!(run(&opened, "s1").await.tools.is_empty());

    let on = config(vec![text("hi")], json!({"tasks": true}));
    assert_eq!(serde_json::to_value(&on).unwrap()["tasks"], json!(true));
    let opened = on.open().await.unwrap();
    assert_eq!(run(&opened, "s1").await.tools, TASKS);
}

#[tokio::test]
async fn the_model_tracks_its_work_without_asking() {
    let turns = vec![
        call(
            "TaskCreate",
            json!({"subject": "Write docs", "description": "All"}),
        ),
        call("TaskUpdate", json!({"id": "1", "status": "completed"})),
        call("TaskList", json!({})),
        text("done"),
        call("TaskList", json!({})),
        text("none here"),
    ];
    let opened = config(turns, json!({"tasks": true})).open().await.unwrap();
    let ran = run(&opened, "s1").await;
    assert_eq!(
        ran.results,
        [
            "Task #1 created: Write docs",
            "Task #1 updated: [completed] Write docs",
            "#1 [completed] Write docs",
        ]
    );
    assert_eq!(ran.final_text.as_deref(), Some("done"), "no approval asked");
    let other = run(&opened, "s2").await;
    assert_eq!(other.results, ["No tasks found."], "tasks are per session");
}

#[tokio::test]
async fn a_subagent_updates_the_tasks_of_the_session_that_started_it() {
    let turns = vec![
        call("TaskCreate", json!({"subject": "Ship", "description": "d"})),
        call(
            "Subagent",
            json!({"description": "go", "prompt": "Finish task 1.", "subagent_type": "implementer"}),
        ),
        call("TaskUpdate", json!({"id": "1", "status": "completed"})),
        text("finished"),
        call("TaskList", json!({})),
        text("done"),
    ];
    let extra = json!({
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "deep_agent": {"durable": false},
    });
    let opened = config(turns, extra).open().await.unwrap();
    let ran = run(&opened, "s1").await;
    assert_eq!(
        ran.results.last().unwrap(),
        "#1 [completed] Ship",
        "{:?}",
        ran.results
    );
}

#[tokio::test]
async fn on_postgres_the_tasks_outlive_the_agent() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping on_postgres_the_tasks_outlive_the_agent: no LINCH_TEST_PG_DSN");
        return;
    };
    let schema = "p6t_tasks";
    let admin = PgPoolOptions::new()
        .max_connections(1)
        .connect(&dsn)
        .await
        .unwrap();
    let reset = [
        format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
        format!("CREATE SCHEMA {schema}"),
    ];
    for sql in reset {
        sqlx::query(AssertSqlSafe(sql))
            .execute(&admin)
            .await
            .unwrap();
    }
    let separator = if dsn.contains('?') { '&' } else { '?' };
    let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");
    let stores = json!({"tasks": true, "stores": {"type": "postgres", "dsn": dsn}});

    let create = json!({"subject": "Write docs", "description": "All"});
    let turns = vec![call("TaskCreate", create), text("made")];
    let opened = config(turns, stores.clone()).open().await.unwrap();
    assert_eq!(
        run(&opened, "s1").await.results,
        ["Task #1 created: Write docs"]
    );
    opened.close().await;
    assert_eq!(rows(&admin, schema).await, [("t".into(), "s1".into(), 1)]);

    let turns = vec![call("TaskGet", json!({"id": "1"})), text("read")];
    let reopened = config(turns, stores).open().await.unwrap();
    assert_eq!(
        run(&reopened, "s1").await.results,
        ["#1 [pending] Write docs\nAll"]
    );
    reopened.close().await;
    let drop = format!("DROP SCHEMA {schema} CASCADE");
    sqlx::query(AssertSqlSafe(drop))
        .execute(&admin)
        .await
        .unwrap();
    admin.close().await;
}

/// Every `(namespace, session_id, task_id)` in the schema's `linch_tasks`.
async fn rows(admin: &PgPool, schema: &str) -> Vec<(String, String, i64)> {
    let sql = format!("SELECT namespace, session_id, task_id FROM {schema}.linch_tasks");
    let query = sqlx::query_as(AssertSqlSafe(sql));
    query.fetch_all(admin).await.unwrap()
}
