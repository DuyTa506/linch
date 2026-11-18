//! `schedule_tools` in an `AgentConfig`: off by default, leaving the tool
//! list, the run contract and the stored config as they were (pinned by a
//! golden digest); on, the facade refuses it unless the stores are
//! Postgres and the host bound the schedule tools.

use std::sync::Arc;

use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Hooks, OpenError, PgPools, ToolRegistry};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn config(extra: Value) -> AgentConfig {
    let turns = [TurnBuilder::new(MODEL).text("ok").build()];
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

/// The tool names and the contract digest of `config`, opened.
async fn opened(config: AgentConfig) -> (Vec<String>, String) {
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    let agent = config
        .open_with(Arc::clone(&tools), hooks, &PgPools::new())
        .await
        .unwrap();
    let names = (tools.snapshot().definitions().into_iter())
        .map(|definition| definition.name)
        .collect();
    (names, agent.agent().contract().to_string())
}

/// The digest the base (before `schedule_tools`) gave this config. A
/// change here changes every existing agent's contract.
const GOLDEN: &str = "3f3a930c3f369c054aae2bc03b6dc29a5f0f98f13f0aafc770ea409f57f2c737";

#[tokio::test]
async fn off_the_tool_list_and_the_contract_are_unchanged() {
    let (names, contract) = opened(config(json!({}))).await;
    assert_eq!(
        names,
        ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "AskUser"]
    );
    assert_eq!(contract, GOLDEN);
}

#[tokio::test]
async fn off_is_the_default_and_is_not_stored() {
    let absent = config(json!({}));
    let off = config(json!({"schedule_tools": false}));
    assert!(!absent.schedule_tools);
    assert_eq!(absent, off);
    // The stored definition (and its digest) is the same as before.
    let stored = off.to_value_with_secrets().unwrap();
    assert!(stored.get("schedule_tools").is_none(), "{stored}");
    assert_eq!(opened(off).await.1, GOLDEN);
    let on = config(json!({"schedule_tools": true}));
    assert_eq!(
        on.to_value_with_secrets().unwrap()["schedule_tools"],
        json!(true)
    );
}

#[tokio::test]
async fn on_the_facade_refuses_it_unless_a_host_bound_the_tools() {
    let on = config(json!({"schedule_tools": true}));
    let refused = on.open().await.unwrap_err();
    assert!(matches!(refused, OpenError::ScheduleTools(_)), "{refused}");
    assert!(refused.to_string().contains("Postgres"), "{refused}");

    // Refused before it connects: this DSN reaches nothing.
    let dsn = "postgres://nobody@127.0.0.1:1/none";
    let on = config(json!({"schedule_tools": true,
                           "stores": {"type": "postgres", "dsn": dsn}}));
    let refused = on.open().await.unwrap_err();
    assert!(matches!(refused, OpenError::ScheduleTools(_)), "{refused}");
    assert!(refused.to_string().contains("linch-server"), "{refused}");
}
