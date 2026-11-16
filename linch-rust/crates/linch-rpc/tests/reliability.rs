//! Provider reliability over RPC: `agent.open` takes the spec's
//! `provider_retry` and `fallback_models`, refuses ones that cannot run,
//! and the run streams its `provider_retry` and `model_fallback` events.

mod common;

use common::*;
use linch::Event;
use linch::ScriptedTurn;
use linch::provider::scripted::ScriptedError;
use linch_rpc::code;
use linch_rpc::protocol::method;
use serde_json::json;

fn overloaded() -> ScriptedTurn {
    ScriptedTurn::failing(ScriptedError::Status {
        status: 529,
        retry_after_ms: None,
        message: "overloaded".into(),
    })
}

#[tokio::test]
async fn a_run_retries_then_fails_over_and_streams_both_events() {
    let mut rpc = Rpc::new();
    let mut config = scripted(vec![overloaded(), overloaded(), text_turn("done")], None, "");
    config["spec"]["provider_retry"] = json!({"max_attempts": 2, "base_delay_ms": 1, "jitter": 0});
    config["spec"]["fallback_models"] = json!(["fallback-model"]);
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let kinds = ended.kinds();
    let at = |kind: &str| kinds.iter().position(|k| k == kind);
    assert!(at("provider_retry") < at("model_fallback"), "{kinds:?}");
    let retry = ended.envelopes.iter().find_map(|e| match &e.event {
        Event::ProviderRetry(retry) => Some(retry.clone()),
        _ => None,
    });
    let retry = retry.expect("a provider_retry event");
    assert_eq!((retry.model.as_str(), retry.retry, retry.delay_ms), (MODEL, 1, 1));
    assert_eq!(retry.error["status"], 529);
    let fallback = ended.envelopes.iter().find_map(|e| match &e.event {
        Event::ModelFallback(fallback) => Some(fallback.clone()),
        _ => None,
    });
    let fallback = fallback.expect("a model_fallback event");
    assert_eq!(fallback.to_model, "fallback-model");
    let Event::Result(result) = ended.last() else {
        panic!("{:?}", ended.last())
    };
    assert_eq!(result.final_text.as_deref(), Some("done"));
}

#[tokio::test]
async fn agent_open_refuses_reliability_settings_that_cannot_run() {
    let rpc = Rpc::new();
    for (key, value) in [
        ("provider_retry", json!({"max_attempts": 0})),
        ("provider_retry", json!({"jitter": 2.0})),
        ("fallback_models", json!([MODEL])),
        ("fallback_models", json!(["a", "a"])),
    ] {
        let mut config = scripted(vec![], None, "");
        config["spec"][key] = value.clone();
        let error = rpc.call(method::AGENT_OPEN, config).await.unwrap_err();
        assert_eq!(code_of(error), code::BUILD_INVALID, "{key}: {value}");
    }
    let mut misspelt = scripted(vec![], None, "");
    misspelt["spec"]["provider_retry"] = json!({"retries": 3});
    let error = rpc.call(method::AGENT_OPEN, misspelt).await.unwrap_err();
    assert_ne!(code_of(error), code::BUILD_INVALID, "a decode error, not a build one");
}
