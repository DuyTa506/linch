//! The reliability policy over RPC: `agent.open` takes `spec.policy`, a run
//! streams its `policy` events, a policy that cannot run is refused, and a
//! tenant may add to the policy its operator requires but not remove it.

mod common;

use common::*;
use linch::Event;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, code};
use serde_json::{Value, json};

/// A client whose host tools answer every call.
fn answering() -> std::sync::Arc<dyn linch_rpc::RequestHandler> {
    handler(|_, _, _| async { Ok(json!({"value": "ok"})) })
}

/// An agent config with the host tools `lookup` and `update`, both
/// read-scope so no call needs an approval.
fn config(turns: Vec<linch::ScriptedTurn>, policy: Value) -> Value {
    let mut config = scripted(turns, None, "");
    config["tools"] = json!([host_tool("lookup", "read"), host_tool("update", "read")]);
    config["spec"]["policy"] = policy;
    config
}

fn policies(ended: &Ended) -> Vec<Value> {
    (ended.envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Policy(event) => Some(serde_json::to_value(event).unwrap()),
            _ => None,
        })
        .collect()
}

fn ordered_and_tested() -> Value {
    json!({
        "prerequisites": [{"tool": "update", "requires": ["lookup"]}],
        "gates": [{"name": "looked-up", "succeeded": ["lookup"]}],
    })
}

#[tokio::test]
async fn a_run_streams_its_policy_events() {
    let mut rpc = Rpc::with_handler(answering());
    let turns = vec![
        tool_turn("c1", "update", json!({"q": "a"})),
        text_turn("done"),
        tool_turn("c2", "lookup", json!({"q": "a"})),
        tool_turn("c3", "update", json!({"q": "b"})),
        text_turn("really done"),
    ];
    let agent = rpc.open(config(turns, ordered_and_tested())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let events = policies(&ended);
    let named: Vec<_> = events
        .iter()
        .map(|e| (e["rule"].as_str().unwrap(), e["action"].as_str().unwrap()))
        .collect();
    assert_eq!(
        named,
        [("prerequisite", "refuse"), ("completion", "correct")]
    );
    assert_eq!(events[0]["tool"], "update");
    assert_eq!(events[0]["missing"], json!(["lookup"]));
    assert_eq!(events[1]["gates"], json!(["looked-up"]));
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(result.final_text.as_deref(), Some("really done"));
}

#[tokio::test]
async fn a_gate_that_never_passes_ends_the_run_failed() {
    let mut rpc = Rpc::with_handler(answering());
    let turns: Vec<_> = (0..5).map(|_| text_turn("done")).collect();
    let agent = rpc.open(config(turns, ordered_and_tested())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let actions: Vec<_> = (policies(&ended).iter())
        .map(|e| e["action"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(actions, ["correct", "correct", "stop"]);
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(serde_json::to_value(result).unwrap()["subtype"], "error");
}

#[tokio::test]
async fn agent_open_refuses_a_policy_that_cannot_run() {
    let rpc = Rpc::new();
    for policy in [
        json!({"prerequisites": [{"tool": "update", "requires": ["nothing"]}]}),
        json!({"prerequisites": [
            {"tool": "update", "requires": ["lookup"]},
            {"tool": "lookup", "requires": ["update"]},
        ]}),
        json!({"gates": [{"name": "g"}]}),
        json!({"gates": [{"name": "g", "check": {"type": "matches", "pattern": "("}}]}),
    ] {
        let config = config(vec![], policy.clone());
        let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
        assert_eq!(code_of(error), code::BUILD_INVALID, "{policy}");
    }
    let misspelt = config(vec![], json!({"gate": []}));
    let error = rpc.call(method::AGENT_OPEN, misspelt).await.unwrap_err();
    assert_ne!(code_of(error), code::BUILD_INVALID, "a decode error");
}

fn acme(allow: Allowlists) -> linch_rpc::Access {
    tenant("acme", allow.scripted_provider(true))
}

fn remote_config(turns: Vec<linch::ScriptedTurn>, policy: Option<Value>) -> Value {
    let mut config = scripted(turns, None, "");
    config["namespace"] = json!("acme");
    config["tools"] = json!([host_tool("lookup", "read"), host_tool("update", "read")]);
    if let Some(policy) = policy {
        config["spec"]["policy"] = policy;
    }
    config
}

/// A tenant's policy is checked on the server on every batch and answer,
/// so its size is bounded; a local connection's is not.
#[tokio::test]
async fn a_tenant_may_not_ask_for_more_policy_than_the_server_bounds() {
    let remote = Rpc::remote(acme(Allowlists::new()));
    let need = |tool: &str| json!({"tool": tool, "requires": ["lookup"]});
    let gate = |i: usize| json!({"name": format!("g{i}"), "succeeded": ["lookup"]});
    let long = "x".repeat(1025);
    let cases = [
        json!({"gates": (0..33).map(gate).collect::<Vec<_>>()}),
        json!({"gates": [{"name": "g", "succeeded": vec!["lookup"; 33]}]}),
        json!({"gates": [{"name": "g", "check": {"type": "forbids", "pattern": long}}]}),
        json!({"gates": [{"name": "g", "succeeded": ["lookup"], "feedback": long}]}),
        json!({"prerequisites": (0..33).map(|_| need("update")).collect::<Vec<_>>()}),
        json!({"max_corrections": 17}),
    ];
    for policy in cases {
        let found = refused(&remote, remote_config(vec![], Some(policy.clone()))).await;
        assert_eq!(found.as_deref(), Some("policy"), "{policy}");
    }
    let within = json!({"gates": (0..32).map(gate).collect::<Vec<_>>(), "max_corrections": 16});
    assert_eq!(
        refused(&remote, remote_config(vec![], Some(within))).await,
        None
    );

    let local = Rpc::new();
    let mut open = config(vec![], json!({"max_corrections": 1000}));
    open["namespace"] = json!("rpc");
    local.open(open).await;
}

fn operators() -> linch::ReliabilityPolicy {
    serde_json::from_value(json!({
        "prerequisites": [{"tool": "update", "requires": ["lookup"]}],
        "gates": [{"name": "looked-up", "succeeded": ["lookup"]}],
        "max_corrections": 1,
    }))
    .unwrap()
}

/// A tenant whose operator requires a policy gets it whatever its config
/// says: with none, with its own, or with a looser cap. It may add rules,
/// not redefine an operator's gate.
#[tokio::test]
async fn a_tenant_cannot_remove_the_policy_its_operator_requires() {
    let required = Allowlists::new().required_policy(Some(operators()));
    let mut rpc = Rpc::remote_with(acme(required), answering());
    // No policy of its own: the operator's applies, so the first answer is
    // corrected and a call of `update` is refused.
    let turns = vec![
        tool_turn("c1", "update", json!({"q": "a"})),
        text_turn("done"),
        tool_turn("c2", "lookup", json!({"q": "a"})),
        text_turn("really done"),
    ];
    let agent = rpc.open(remote_config(turns, None)).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let actions: Vec<_> = (policies(&ended).iter())
        .map(|e| e["action"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(actions, ["refuse", "correct"], "{:?}", ended.error);

    // Its own policy adds a gate, asks for more corrections, and drops
    // nothing: the cap stays the operator's, the prerequisite stays.
    let own = json!({
        "gates": [{"name": "polite", "check": {"type": "keywords", "keywords": ["thanks"]}}],
        "max_corrections": 9,
    });
    let turns = vec![
        tool_turn("c1", "update", json!({"q": "a"})),
        text_turn("done"),
        text_turn("done"),
        text_turn("done"),
    ];
    let agent = rpc.open(remote_config(turns, Some(own))).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let events = policies(&ended);
    let actions: Vec<_> = events
        .iter()
        .map(|e| e["action"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(actions, ["refuse", "correct", "stop"], "one correction");
    assert_eq!(events[1]["gates"], json!(["looked-up", "polite"]));

    // Redefining the operator's gate is refused; repeating it is not.
    let redefined = json!({"gates": [{"name": "looked-up", "check":
        {"type": "keywords", "keywords": ["x"]}}]});
    let remote = Rpc::remote(acme(Allowlists::new().required_policy(Some(operators()))));
    let found = refused(&remote, remote_config(vec![], Some(redefined))).await;
    assert_eq!(found.as_deref(), Some("policy"));
    let same = json!({"gates": [{"name": "looked-up", "succeeded": ["lookup"]}]});
    assert_eq!(
        refused(&remote, remote_config(vec![], Some(same))).await,
        None
    );
}

/// A tenant policy that leaves `max_corrections` out does not lower the
/// operator's cap to the default; one that sets it may only go below it.
#[tokio::test]
async fn an_absent_tenant_cap_keeps_the_operators() {
    let operator: linch::ReliabilityPolicy = serde_json::from_value(json!({
        "gates": [{"name": "looked-up", "succeeded": ["lookup"]}],
        "max_corrections": 3,
    }))
    .unwrap();
    let mut rpc = Rpc::remote_with(
        acme(Allowlists::new().required_policy(Some(operator))),
        answering(),
    );
    let own = json!({"gates": [{"name": "polite", "check":
        {"type": "keywords", "keywords": ["thanks"]}}]});
    let turns = (0..6).map(|_| text_turn("done")).collect();
    let agent = rpc.open(remote_config(turns, Some(own))).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let actions: Vec<_> = (policies(&ended).iter())
        .map(|e| e["action"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(actions, ["correct", "correct", "correct", "stop"]);

    // Its own, lower cap still applies; a higher one does not raise it.
    for (own_cap, corrections) in [(1, 1), (9, 3)] {
        let own = json!({"max_corrections": own_cap});
        let turns = (0..6).map(|_| text_turn("done")).collect();
        let agent = rpc.open(remote_config(turns, Some(own))).await;
        let run_id = rpc.start(&agent, "s1", "go").await;
        let ended = rpc.until_end(&run_id).await;
        let spent = (policies(&ended).iter())
            .filter(|e| e["action"] == "correct")
            .count();
        assert_eq!(spent, corrections, "own cap {own_cap}");
    }
}

/// An operator rule naming a tool the agent lacks refuses the open: it is
/// never dropped.
#[tokio::test]
async fn a_required_policy_the_agent_cannot_meet_refuses_the_open() {
    let required = Allowlists::new().required_policy(Some(operators()));
    let remote = Rpc::remote(acme(required));
    let mut toolless = remote_config(vec![], None);
    toolless["tools"] = json!([]);
    let error = remote
        .call(method::AGENT_OPEN, toolless.clone())
        .await
        .unwrap_err();
    // The tenant is not told which tools the operator's rules name.
    let message = match &error {
        linch_rpc::ClientError::Rpc(object) => object.message.clone(),
        other => panic!("{other:?}"),
    };
    assert!(message.starts_with("policy:"), "{message}");
    assert!(!message.contains("update") && !message.contains("lookup"));
    assert_eq!(code_of(error), code::BUILD_INVALID);

    // A local connection has no tenant: its error names the tool.
    let local = Rpc::new();
    let mut open = config(
        vec![],
        json!({"gates": [{"name": "g", "succeeded": ["nope"]}]}),
    );
    open["namespace"] = json!("rpc");
    let error = local.call(method::AGENT_OPEN, open).await.unwrap_err();
    match error {
        linch_rpc::ClientError::Rpc(object) => assert!(object.message.contains("nope")),
        other => panic!("{other:?}"),
    }
}
