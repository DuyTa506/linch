//! Redaction and verifiers over RPC: `agent.open` takes the spec's
//! `redaction` and `verification`, refuses ones that cannot run, a tenant
//! may not ask for more than the server bounds, and a run streams only the
//! redacted text and its `verification` events.

mod common;

use common::*;
use linch::Event;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, code};
use serde_json::{Value, json};

const SECRET: &str = "sk-live0123456789";

fn redaction() -> Value {
    json!({"rules": [{"pattern": "sk-[A-Za-z0-9]{8,}", "replacement": "[KEY]"}]})
}

#[tokio::test]
async fn a_run_streams_only_the_redacted_text() {
    let mut rpc = Rpc::new();
    let turn = TurnBuilder::new(MODEL)
        .text(format!("the key is {SECRET}"))
        .build();
    let mut config = scripted(vec![turn], None, "");
    config["spec"]["redaction"] = redaction();
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let streamed = serde_json::to_string(&ended.envelopes).unwrap();
    assert!(!streamed.contains(SECRET), "{streamed}");
    assert!(!ended.kinds().contains(&"partial_assistant".to_owned()));
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(result.final_text.as_deref(), Some("the key is [KEY]"));
}

#[tokio::test]
async fn a_failed_answer_is_verified_again_and_streams_its_event() {
    let mut rpc = Rpc::new();
    let mut config = scripted(vec![text_turn("no"), text_turn("see [1]")], None, "");
    config["spec"]["verification"] = json!({"verifiers": [
        {"name": "cites", "check": {"type": "matches", "pattern": "\\[\\d+\\]"}}
    ]});
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let verification = ended.envelopes.iter().find_map(|e| match &e.event {
        Event::Verification(event) => Some(event.clone()),
        _ => None,
    });
    let verification = verification.expect("a verification event");
    assert_eq!(
        (verification.verifier.as_str(), verification.attempt),
        ("cites", 1)
    );
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(result.final_text.as_deref(), Some("see [1]"));
}

#[tokio::test]
async fn agent_open_refuses_guardrails_that_cannot_run() {
    let rpc = Rpc::new();
    for (key, value) in [
        ("redaction", json!({"rules": []})),
        ("redaction", json!({"rules": [{"pattern": "("}]})),
        ("redaction", json!({"rules": [{"pattern": "x*"}]})),
        (
            "redaction",
            json!({"rules": [{"pattern": "x"}], "response": false, "tool_results": false}),
        ),
        (
            "verification",
            json!({"verifiers": [{"name": "output_schema",
                                  "check": {"type": "matches", "pattern": "x"}}]}),
        ),
        (
            "verification",
            json!({"verifiers": [{"name": "k", "check": {"type": "keywords", "keywords": []}}]}),
        ),
    ] {
        let mut config = scripted(vec![], None, "");
        config["spec"][key] = value.clone();
        let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
        assert_eq!(code_of(error), code::BUILD_INVALID, "{key}: {value}");
    }
    let mut misspelt = scripted(vec![], None, "");
    misspelt["spec"]["verification"] = json!({"retries": 3});
    let error = rpc.call(method::AGENT_OPEN, misspelt).await.unwrap_err();
    assert_ne!(code_of(error), code::BUILD_INVALID, "a decode error");
}

/// A tenant's patterns are compiled and run on the server, so their number
/// and length are bounded; a local connection is not.
#[tokio::test]
async fn a_tenant_may_not_ask_for_more_patterns_than_the_server_bounds() {
    let remote = Rpc::remote(tenant("acme", Allowlists::new().scripted_provider(true)));
    let config = |spec: Value| {
        let mut config = scripted(vec![], None, "");
        config["namespace"] = json!("acme");
        for (key, value) in spec.as_object().unwrap() {
            config["spec"][key] = value.clone();
        }
        config
    };
    let rule = |pattern: &str| json!({"pattern": pattern});
    let verifier = |i: usize| {
        json!({"name": format!("v{i}"),
                                     "check": {"type": "matches", "pattern": "x"}})
    };
    let within = json!({
        "redaction": {"rules": vec![rule("x"); 32]},
        "verification": {"verifiers": (0..32).map(verifier).collect::<Vec<_>>()},
    });
    assert_eq!(refused(&remote, config(within.clone())).await, None);
    let many_rules = json!({"redaction": {"rules": vec![rule("x"); 33]}});
    let long = "x".repeat(1025);
    let long_rule = json!({"redaction": {"rules": [rule(&long)]}});
    let many_verifiers =
        json!({"verification": {"verifiers": (0..33).map(verifier).collect::<Vec<_>>()}});
    let long_verifier = json!({"verification": {"verifiers": [
        {"name": "v", "check": {"type": "forbids", "pattern": long}}
    ]}});
    let keywords = json!({"verification": {"verifiers": [
        {"name": "v", "check": {"type": "keywords", "keywords": vec!["k"; 33]}}
    ]}});
    for (spec, kind) in [
        (many_rules, "redaction"),
        (long_rule, "redaction"),
        (many_verifiers, "verification"),
        (long_verifier, "verification"),
        (keywords, "verification"),
    ] {
        let found = refused(&remote, config(spec.clone())).await;
        assert_eq!(found.as_deref(), Some(kind), "{spec}");
    }
    let local = Rpc::new();
    let mut open = config(json!({"redaction": {"rules": vec![rule("x"); 40]}}));
    open["namespace"] = json!("rpc");
    local.open(open).await;
}
