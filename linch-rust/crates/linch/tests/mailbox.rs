//! The mailbox an `AgentConfig` turns on: in process with memory stores,
//! and on Postgres stores a table, so a message outlives the agent that
//! sent it (skipped without `LINCH_TEST_PG_DSN`).

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, OpenedAgent, RunOptions};
use serde_json::{Value, json};
use sqlx::AssertSqlSafe;
use sqlx::postgres::PgPoolOptions;

const MODEL: &str = "scripted-model";

fn send(to: &str, content: &str) -> Value {
    let input = json!({"to": to, "content": content});
    let turn = TurnBuilder::new(MODEL).tool_call("call_1", "send_message", &input);
    json!(turn.build())
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "mailbox": true,
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

/// The text blocks of the prompt a run of `session` logged.
async fn prompt(opened: &OpenedAgent, session: &str) -> Vec<String> {
    let mut stream = opened.agent().run("go", RunOptions::new(session));
    let mut blocks = Vec::new();
    while let Some(item) = stream.next().await {
        if let Event::User(user) = item.unwrap().event
            && blocks.is_empty()
        {
            let value = serde_json::to_value(&user.message).unwrap();
            let content = value["content"].as_array().unwrap();
            let texts = content.iter().map(|block| block["text"].as_str());
            blocks = texts
                .map(|text| text.unwrap_or_default().to_owned())
                .collect();
        }
    }
    blocks
}

const PEER: &str = "<peer-message><from>lead</from><type>message</type>\
                    <content>plan?</content></peer-message>";

#[tokio::test]
async fn with_memory_stores_sessions_of_one_agent_exchange_messages() {
    let turns = vec![send("alice", "plan?"), text("sent"), text("ok")];
    let opened = config(turns, json!({})).open().await.unwrap();
    prompt(&opened, "lead").await;
    assert_eq!(prompt(&opened, "alice").await, ["go", PEER]);
    opened.close().await;
}

#[tokio::test]
async fn on_postgres_a_message_outlives_the_agent_that_sent_it() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping on_postgres_a_message_outlives_the_agent_that_sent_it");
        return;
    };
    let schema = "m1_facade_mailbox";
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
    let stores = json!({"stores": {"type": "postgres", "dsn": dsn}});

    let lead = config(vec![send("alice", "plan?"), text("sent")], stores.clone());
    let lead = lead.open().await.unwrap();
    prompt(&lead, "lead").await;
    lead.close().await;

    let alice = config(vec![text("ok"), text("again")], stores);
    let alice = alice.open().await.unwrap();
    assert_eq!(prompt(&alice, "alice").await, ["go", PEER]);
    assert_eq!(prompt(&alice, "alice").await, ["go"], "delivered once");
    alice.close().await;
    let sql = format!("SELECT count(*) FROM {schema}.linch_mailbox");
    let left: i64 = sqlx::query_scalar(AssertSqlSafe(sql))
        .fetch_one(&admin)
        .await
        .unwrap();
    assert_eq!(left, 0);
    let drop = format!("DROP SCHEMA {schema} CASCADE");
    sqlx::query(AssertSqlSafe(drop))
        .execute(&admin)
        .await
        .unwrap();
    admin.close().await;
}
