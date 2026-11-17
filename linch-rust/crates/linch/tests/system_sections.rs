//! `spec.system` on an `AgentConfig`: sections and `append` reach the
//! requests, and `replace_defaults` leaves out the text the presets (deep
//! agent, filesystem tools, profile) would add to the system prompt.

use futures::StreamExt;
use linch::ext::deep_agent::DEEP_AGENT_SYSTEM_PROMPT;
use linch::provider::scripted::TurnBuilder;
use linch::tools::vfs::PROMPT;
use linch::{AgentConfig, RunOptions};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn config(system: Value) -> AgentConfig {
    let reply = json!(TurnBuilder::new(MODEL).text("hi").build());
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": MODEL, "system_prompt": "Answer in French.", "system": system},
        "provider": {"id": "scripted", "turns": [reply]},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "filesystem": {},
        "deep_agent": {"durable": false},
        "profile": {"id": "support", "version": "1", "system_prompt": "Help customers."},
    }))
    .unwrap()
}

#[tokio::test]
async fn the_presets_text_is_added_unless_the_spec_replaces_the_defaults() {
    let kept = config(json!({})).open().await.unwrap();
    let prompt = &kept.agent().spec().system_prompt;
    assert!(prompt.contains(DEEP_AGENT_SYSTEM_PROMPT), "{prompt}");
    assert!(prompt.contains("Help customers."), "{prompt}");
    assert!(prompt.contains(PROMPT), "{prompt}");

    let replaced = config(json!({"replace_defaults": true}));
    let opened = replaced.open().await.unwrap();
    let prompt = &opened.agent().spec().system_prompt;
    assert_eq!(prompt, "Answer in French.");
    assert_ne!(
        opened.agent().contract(),
        kept.agent().contract(),
        "the flag is part of the contract"
    );
}

#[tokio::test]
async fn sections_and_append_round_trip_through_the_config_and_the_agent_runs() {
    let system = json!({
        "sections": [
            {"name": "tone", "text": "Tone.", "order": 2},
            {"name": "policy", "text": "Policy.", "order": 1},
        ],
        "append": "Last word.",
        "replace_defaults": true,
    });
    let echoed = serde_json::to_value(config(system.clone())).unwrap();
    assert_eq!(echoed["spec"]["system"], system);
    let opened = config(system).open().await.unwrap();
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    while stream.next().await.is_some() {}
    let spec = opened.agent().spec();
    assert_eq!(spec.system.sections.len(), 2);
}
