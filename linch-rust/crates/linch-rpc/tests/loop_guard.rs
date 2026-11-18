//! The loop guard over RPC: `agent.open` takes `spec.loop_guard`, a stuck
//! run streams its `loop_guard` event and ends failed, and a tenant may not
//! let a run take more turns without a provider call than the server
//! bounds.

mod common;

use common::*;
use linch::Event;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, code};
use serde_json::{Value, json};

/// Ten responses calling a tool the agent does not have.
fn missing() -> Vec<linch::ScriptedTurn> {
    let call = |t: usize| tool_turn(&format!("c{t}"), "Missing", json!({"n": t}));
    (0..10).map(call).collect()
}

#[tokio::test]
async fn a_stuck_run_streams_its_loop_guard_and_fails() {
    let mut rpc = Rpc::new();
    let agent = rpc.open(scripted(missing(), None, "")).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let guard = ended.envelopes.iter().find_map(|e| match &e.event {
        Event::LoopGuard(event) => Some(serde_json::to_value(event).unwrap()),
        _ => None,
    });
    let guard = guard.expect("a loop_guard event");
    assert_eq!(guard["reason"], "repeated_failures");
    assert_eq!(guard["action"], "stop");
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(serde_json::to_value(result).unwrap()["subtype"], "error");

    // Off, the same calls run on to `max_turns`.
    let mut config = scripted(missing(), None, "");
    config["spec"]["max_turns"] = json!(5);
    config["spec"]["loop_guard"] = json!({"max_consecutive_failures": 0});
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let reasons: Vec<Value> = (ended.envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::LoopGuard(event) => Some(serde_json::to_value(event).unwrap()["reason"].clone()),
            _ => None,
        })
        .collect();
    assert_eq!(reasons, [json!("max_turns")]);
}

#[tokio::test]
async fn agent_open_refuses_a_loop_guard_that_cannot_run() {
    let rpc = Rpc::new();
    let mut config = scripted(vec![], None, "");
    config["spec"]["loop_guard"] = json!({"max_idle_turns": 0});
    let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
    assert_eq!(code_of(error), code::BUILD_INVALID);
    let mut unknown = scripted(vec![], None, "");
    unknown["spec"]["loop_guard"] = json!({"force_final_answer": true});
    let error = rpc.call(method::AGENT_OPEN, unknown).await.unwrap_err();
    assert_ne!(code_of(error), code::BUILD_INVALID, "a decode error");
}

/// A turn without a provider call is a write and an append on the server
/// that no budget counts, so a tenant may lower the bound but not raise it;
/// a local connection may.
#[tokio::test]
async fn a_tenant_may_not_raise_the_idle_turn_bound() {
    let remote = Rpc::remote(tenant("acme", Allowlists::new().scripted_provider(true)));
    let config = |guard: Value| {
        let mut config = scripted(vec![], None, "");
        config["namespace"] = json!("acme");
        config["spec"]["loop_guard"] = guard;
        config
    };
    for within in [
        json!({}),
        json!({"max_idle_turns": 25}),
        json!({"max_idle_turns": 3}),
    ] {
        assert_eq!(
            refused(&remote, config(within.clone())).await,
            None,
            "{within}"
        );
    }
    let raised = config(json!({"max_idle_turns": 26}));
    assert_eq!(
        refused(&remote, raised).await.as_deref(),
        Some("loop_guard")
    );
    let local = Rpc::new();
    let mut open = config(json!({"max_idle_turns": 100}));
    open["namespace"] = json!("rpc");
    local.open(open).await;
}
