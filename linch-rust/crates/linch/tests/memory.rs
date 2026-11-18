//! `memory` in an `AgentConfig`: off by default, leaving the tool list, the
//! run contract and the stored config as they were (pinned by a golden
//! digest); on, the model gets the memory tools, and `recall` adds what a
//! search finds to each request without touching the log.

use std::sync::Arc;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{
    AgentConfig, Event, Hooks, OpenError, OpenedAgent, PgPools, Principal, RunOptions, ToolRegistry,
};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL, "system_prompt": "Keep a schedule."},
        "provider": {"id": "scripted", "turns": turns},
        "tasks": true,
        "ask_user": true,
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

fn call(id: &str, tool: &str, input: Value) -> Value {
    json!(TurnBuilder::new(MODEL).tool_call(id, tool, &input).build())
}

/// The tool names and the contract digest of `config`, opened.
async fn opened(config: AgentConfig) -> (Vec<String>, String) {
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    let agent = (config
        .open_with(Arc::clone(&tools), hooks, &PgPools::new())
        .await)
        .unwrap();
    let names = (tools.snapshot().definitions().into_iter())
        .map(|definition| definition.name)
        .collect();
    (names, agent.agent().contract().to_string())
}

/// The digest the base (before `memory`) gave this config. A change here
/// changes every existing agent's contract.
const GOLDEN: &str = "3f3a930c3f369c054aae2bc03b6dc29a5f0f98f13f0aafc770ea409f57f2c737";

#[tokio::test]
async fn off_the_tool_list_and_the_contract_are_unchanged() {
    let (names, contract) = opened(config(vec![], json!({}))).await;
    assert_eq!(
        names,
        ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "AskUser"]
    );
    assert_eq!(contract, GOLDEN);
}

#[tokio::test]
async fn off_is_the_default_and_is_not_stored() {
    let absent = config(vec![], json!({}));
    assert!(absent.memory.is_none());
    let stored = absent.to_value_with_secrets().unwrap();
    assert!(stored.get("memory").is_none(), "{stored}");
    // An unset field stays off the wire, so a plain `memory` adds nothing
    // but itself.
    let plain = config(vec![], json!({"memory": {}}));
    let stored = plain.to_value_with_secrets().unwrap();
    assert_eq!(stored["memory"], json!({}));
    let full = config(
        vec![],
        json!({"memory": {"scope": "agent", "recall": {"k": 3}, "max_memories": 10,
                              "max_memories_per_scope": 4}}),
    );
    let stored = full.to_value_with_secrets().unwrap();
    assert_eq!(
        stored["memory"],
        json!({"scope": "agent", "recall": {"k": 3}, "max_memories": 10,
               "max_memories_per_scope": 4})
    );
    let again: AgentConfig = serde_json::from_value(stored).unwrap();
    assert_eq!(again, full);
}

#[tokio::test]
async fn unknown_memory_fields_are_refused() {
    for extra in [
        json!({"memory": {"namespace": "other"}}),
        json!({"memory": {"scope": "global"}}),
        json!({"memory": {"recall": {"k": 3, "scope": "agent"}}}),
    ] {
        let mut value = json!({
            "namespace": "t",
            "spec": {"model": MODEL},
            "provider": {"id": "scripted", "turns": []},
        });
        value
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        assert!(serde_json::from_value::<AgentConfig>(value).is_err());
    }
}

#[tokio::test]
async fn on_it_adds_the_tools_and_recall_changes_the_contract() {
    let (names, tools_only) = opened(config(vec![], json!({"memory": {}}))).await;
    assert_eq!(
        names,
        [
            "TaskCreate",
            "TaskList",
            "TaskGet",
            "TaskUpdate",
            "AskUser",
            "MemorySave",
            "MemorySearch",
            "MemoryDelete"
        ]
    );
    assert_ne!(tools_only, GOLDEN);
    let recall = |k: u32, scope: &str| {
        let memory = json!({"memory": {"scope": scope, "recall": {"k": k}}});
        opened(config(vec![], memory))
    };
    let (_, k5) = recall(5, "agent").await;
    assert_ne!(k5, tools_only, "recall is in the contract");
    assert_ne!(k5, recall(6, "agent").await.1, "k is in the contract");
    assert_ne!(k5, recall(5, "principal").await.1, "the scope is too");
    assert_eq!(k5, recall(5, "agent").await.1);
}

#[tokio::test]
async fn an_invalid_memory_config_does_not_open() {
    for memory in [
        json!({"recall": {"k": 0}}),
        json!({"recall": {"k": 21}}),
        json!({"max_memories": 0}),
        json!({"max_memories_per_scope": 2}),
        json!({"max_memories": 4, "max_memories_per_scope": 0}),
        json!({"max_memories": 4, "max_memories_per_scope": 5}),
    ] {
        let refused = config(vec![], json!({"memory": memory}))
            .open()
            .await
            .unwrap_err();
        assert!(matches!(refused, OpenError::Memory(_)), "{refused}");
    }
}

/// What one run showed.
struct Ran {
    results: Vec<String>,
    /// `metadata.memory` of each `context_build` event.
    recalled: Vec<Value>,
    final_text: Option<String>,
}

async fn run(
    opened: &OpenedAgent,
    session: &str,
    prompt: &str,
    principal: Option<Principal>,
) -> Ran {
    let mut options = RunOptions::new(session);
    options.principal = principal;
    let mut stream = opened.agent().run(prompt, options);
    let mut ran = Ran {
        results: Vec::new(),
        recalled: Vec::new(),
        final_text: None,
    };
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::ToolCallEnd(end) => {
                let result = serde_json::to_value(&end.result).unwrap();
                let text = result["content"][0]["text"].as_str().unwrap_or_default();
                ran.results.push(text.to_owned());
            }
            Event::ContextBuild(build) => {
                ran.recalled.extend(build.metadata.get("memory").cloned());
            }
            Event::Result(result) => ran.final_text = result.final_text,
            _ => {}
        }
    }
    ran
}

fn ann() -> Principal {
    Principal::new("acme", "ann", ["user"]).unwrap()
}

fn bob() -> Principal {
    Principal::new("acme", "bob", ["user"]).unwrap()
}

/// A scripted agent that may write memory without asking.
fn memory_agent(turns: Vec<Value>, memory: Value) -> AgentConfig {
    config(
        turns,
        json!({
            "memory": memory,
            "permissions": {"mode": "accept_edits", "project_root": "/"},
        }),
    )
}

#[tokio::test]
async fn a_memory_saved_in_one_session_is_recalled_in_another() {
    let turns = vec![
        call(
            "c1",
            "MemorySave",
            json!({"content": "Ann's favourite tea is oolong", "tags": ["tea"]}),
        ),
        text("saved"),
        text("oolong"),
    ];
    let opened = memory_agent(turns, json!({"scope": "agent", "recall": {"k": 3}}))
        .open()
        .await
        .unwrap();
    let first = run(&opened, "s1", "Remember my favourite tea.", None).await;
    assert!(
        first.results[0].starts_with("Saved memory "),
        "{:?}",
        first.results
    );
    // The run's second request already finds what its first turn saved.
    assert_eq!(first.recalled.len(), 1);

    let second = run(&opened, "s2", "Which tea is Ann's favourite?", None).await;
    assert_eq!(second.final_text.as_deref(), Some("oolong"));
    let ids = second.recalled[0]["ids"].as_array().unwrap();
    assert_eq!(ids.len(), 1, "{:?}", second.recalled);
    let saved = first.results[0].strip_prefix("Saved memory ").unwrap();
    assert_eq!(ids[0], saved);
}

#[tokio::test]
async fn principal_memory_is_recalled_only_for_its_principal() {
    let turns = vec![
        call(
            "c1",
            "MemorySave",
            json!({"content": "Ann likes oolong tea"}),
        ),
        text("saved"),
        text("nothing for bob"),
        text("oolong"),
        text("no principal"),
        call("c2", "MemorySearch", json!({"query": "tea"})),
        text("denied"),
    ];
    let memory = json!({"scope": "principal", "recall": {"k": 3}});
    let opened = memory_agent(turns, memory).open().await.unwrap();
    run(&opened, "s1", "Remember my favourite tea.", Some(ann())).await;

    let bobs = run(&opened, "s2", "Which tea is Ann's favourite?", Some(bob())).await;
    assert!(bobs.recalled.is_empty(), "bob recalls nothing of ann's");
    let anns = run(&opened, "s3", "Which tea is Ann's favourite?", Some(ann())).await;
    assert_eq!(anns.recalled.len(), 1);
    // No principal: no recall, and the tools fail closed.
    let nobody = run(&opened, "s4", "Which tea is Ann's favourite?", None).await;
    assert!(nobody.recalled.is_empty(), "no principal recalls nothing");
    let denied = run(&opened, "s5", "Search tea.", None).await;
    assert!(
        denied.results[0].contains("per principal"),
        "{:?}",
        denied.results
    );
}

#[tokio::test]
async fn on_postgres_the_memories_outlive_the_agent_and_obey_the_cap() {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping on_postgres_the_memories_outlive_the_agent: no LINCH_TEST_PG_DSN");
        return;
    };
    let schema = "p6t_memory";
    let admin = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&dsn)
        .await
        .unwrap();
    for sql in [
        format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
        format!("CREATE SCHEMA {schema}"),
    ] {
        let query = sqlx::query(sqlx::AssertSqlSafe(sql));
        query.execute(&admin).await.unwrap();
    }
    let separator = if dsn.contains('?') { '&' } else { '?' };
    let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");
    let stores = json!({"type": "postgres", "dsn": dsn});
    let memory = json!({"scope": "agent", "recall": {"k": 3}, "max_memories": 1});
    let extra = json!({
        "memory": memory,
        "stores": stores,
        "permissions": {"mode": "accept_edits", "project_root": "/"},
    });

    let turns = vec![
        call(
            "c1",
            "MemorySave",
            json!({"content": "Ann prefers oolong tea"}),
        ),
        call("c2", "MemorySave", json!({"content": "a second fact"})),
        text("saved"),
    ];
    let opened = config(turns, extra.clone()).open().await.unwrap();
    let first = run(&opened, "s1", "Remember.", None).await;
    let saved = &first.results;
    assert!(saved[0].starts_with("Saved memory "), "{saved:?}");
    assert!(saved[1].contains("limit of 1"), "{saved:?}");
    opened.close().await;

    // Another agent on the same database and namespace finds it.
    let reopened = config(vec![text("oolong")], extra).open().await.unwrap();
    let second = run(
        &reopened,
        "other-session",
        "Which tea is Ann's favourite?",
        None,
    )
    .await;
    assert_eq!(second.recalled[0]["ids"].as_array().unwrap().len(), 1);
    reopened.close().await;
    let drop = format!("DROP SCHEMA {schema} CASCADE");
    let query = sqlx::query(sqlx::AssertSqlSafe(drop));
    query.execute(&admin).await.unwrap();
}
