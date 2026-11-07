//! The `bash` switch: a workspace agent has `Bash` unless its config turns
//! it off, and the switch is left out of a config's echo while it is on.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};

/// The tool names of a scripted workspace agent with `extra` added to its
/// config.
async fn tools(extra: Value) -> Vec<String> {
    let dir = tempfile::tempdir().unwrap();
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted",
                     "turns": [TurnBuilder::new("scripted-model").text("ok").build()]},
        "workspace": dir.path(),
    });
    for (key, field) in extra.as_object().unwrap() {
        value[key] = field.clone();
    }
    let config: AgentConfig = serde_json::from_value(value).unwrap();
    let opened = config.open().await.unwrap();
    let mut stream = opened.agent().run("hi", RunOptions::new("s1"));
    let first = stream.next().await.unwrap().unwrap().event;
    let Event::Init(init) = first else {
        panic!("first event {first:?}");
    };
    init.tools
}

#[tokio::test]
async fn bash_is_a_workspace_tool_unless_the_config_turns_it_off() {
    let all = tools(json!({})).await;
    assert!(all.iter().any(|tool| tool == "Bash"), "{all:?}");
    assert_eq!(tools(json!({"bash": true})).await, all);
    let without = tools(json!({"bash": false})).await;
    let expected: Vec<String> = all.into_iter().filter(|tool| tool != "Bash").collect();
    assert_eq!(without, expected);
}

#[test]
fn the_switch_is_echoed_only_when_it_is_off() {
    let config = |extra: Value| {
        let mut value = json!({"namespace": "t", "spec": {"model": "m"},
                               "provider": {"id": "deepseek"}});
        for (key, field) in extra.as_object().unwrap() {
            value[key] = field.clone();
        }
        serde_json::from_value::<AgentConfig>(value).unwrap()
    };
    let on = config(json!({}));
    assert!(on.bash);
    assert!(serde_json::to_value(&on).unwrap().get("bash").is_none());
    let off = serde_json::to_value(config(json!({"bash": false}))).unwrap();
    assert_eq!(off["bash"], json!(false));
}
