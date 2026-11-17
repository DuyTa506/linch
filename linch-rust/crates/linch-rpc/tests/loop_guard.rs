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

/// A model forced to call a tool every turn never answers, so a tenant's
/// forced choice needs something that ends the run: `max_turns` or a final
/// tool, named in the spec or, for a run's own choice, on its agent.
#[tokio::test]
async fn a_tenant_may_not_force_a_tool_nothing_would_end() {
    let remote = Rpc::remote(tenant("acme", Allowlists::new().scripted_provider(true)));
    let config = |spec: Value| {
        let mut config = scripted(vec![], None, "");
        config["namespace"] = json!("acme");
        config["tools"] = json!([host_tool("lookup", "read")]);
        // A token cap bounds the run for admission without ending it by turns.
        config["spec"] = json!({"model": MODEL, "budget": {"max_tokens": 1_000_000}});
        for (key, value) in spec.as_object().unwrap() {
            config["spec"][key] = value.clone();
        }
        config
    };
    let required = json!({"type": "required"});
    let named = json!({"type": "tool", "name": "lookup"});
    // An output schema alone is no end: its final tool exists only on a
    // provider that cannot take the schema natively.
    let schema = json!({"name": "answer", "schema": {"type": "object"}});
    for spec in [
        json!({"tool_choice": required}),
        json!({"tool_choice": named}),
        json!({"tool_choice": required, "output_schema": schema}),
    ] {
        let kind = refused(&remote, config(spec.clone())).await;
        assert_eq!(kind.as_deref(), Some("tool_choice"), "{spec}");
    }
    for spec in [
        json!({"tool_choice": required, "max_turns": 5}),
        json!({"tool_choice": {"type": "auto"}}),
        json!({"tool_choice": named, "final_tool_name": "lookup"}),
        json!({"tool_choice": required, "final_tool_name": "lookup"}),
    ] {
        assert_eq!(refused(&remote, config(spec.clone())).await, None, "{spec}");
    }

    // A run's own choice is held to the same rule by its agent.
    let start = |agent: &str, choice: &Value| {
        let params = json!({"agent": agent, "session_id": "s1", "prompt": "go",
                            "tool_choice": choice});
        remote.call(method::RUN_START, params)
    };
    let loose = remote.open(config(json!({}))).await;
    let error = start(&loose, &required).await.unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("tool_choice".into()))
    );
    assert!(start(&loose, &json!({"type": "auto"})).await.is_ok());
    // The same on an agent with an output schema, as at open.
    let schemed = remote.open(config(json!({"output_schema": schema}))).await;
    let error = start(&schemed, &required).await.unwrap_err();
    assert_eq!(refusal(error).1.as_deref(), Some("tool_choice"));
    let bounded = remote.open(config(json!({"max_turns": 3}))).await;
    assert!(start(&bounded, &required).await.is_ok());

    // A local connection is not held to it.
    let local = Rpc::new();
    let mut open = config(json!({"tool_choice": required}));
    open["namespace"] = json!("rpc");
    local.open(open).await;
}

/// A host tool that declares it polls is waited on, as `BashOutput` is: the
/// same answer eight times is no loop. One that does not declare it is, at
/// the third. A poll whose answer says it is done is a repeat again.
#[tokio::test]
async fn a_host_tool_may_declare_that_it_polls() {
    let turns = || {
        let mut turns: Vec<_> = (0..8)
            .map(|n| tool_turn(&format!("c{n}"), "status", json!({"q": "job"})))
            .collect();
        turns.push(text_turn("Done."));
        turns
    };
    let run = |polls: bool, done: bool| async move {
        let answer = match done {
            true => json!({"value": "finished", "metadata": {"poll_done": true}}),
            false => json!({"value": "running"}),
        };
        let mut rpc = Rpc::with_handler(handler(move |_, _, _| {
            let answer = answer.clone();
            async move { Ok(answer) }
        }));
        let mut tool = host_tool("status", "read");
        if polls {
            tool["polls"] = json!(true);
        }
        let config = hosted(turns(), "default", json!({"tools": [tool]}));
        let agent = rpc.open(config).await;
        let run_id = rpc.start(&agent, "s1", "wait").await;
        let ended = rpc.until_end(&run_id).await;
        let Event::Result(result) = ended.last() else {
            panic!("{:?}", ended.last())
        };
        let ends = (ended.envelopes.iter())
            .filter(|e| matches!(e.event, Event::ToolCallEnd(_)))
            .count();
        (
            serde_json::to_value(result).unwrap()["subtype"].clone(),
            ends,
        )
    };
    assert_eq!(run(true, false).await, (json!("success"), 8));
    assert_eq!(run(false, false).await, (json!("error"), 3));
    assert_eq!(run(true, true).await, (json!("error"), 3));
}
