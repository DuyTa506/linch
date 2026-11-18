//! Provider request settings stay out of what a config's other parts mean:
//! an unset one is not on the wire, and a set one is not part of the run
//! contract (like `base_url`, it is operational).

use linch::{AgentConfig, OpenError};
use serde_json::{Value, json};

fn config(provider: Value) -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": provider,
    }))
    .unwrap()
}

#[test]
fn a_provider_without_settings_serializes_without_request_or_chat() {
    for id in [
        "deepseek",
        "openai-chat",
        "openai-responses",
        "anthropic",
        "gemini",
    ] {
        let wire = serde_json::to_value(config(json!({"id": id}))).unwrap();
        let provider = wire["provider"].as_object().unwrap();
        assert!(
            !provider.contains_key("request") && !provider.contains_key("chat"),
            "{id}: {provider:?}"
        );
    }
    let vllm = json!({"id": "vllm", "base_url": "http://127.0.0.1:9/v1"});
    let wire = serde_json::to_value(config(vllm.clone())).unwrap();
    assert_eq!(wire["provider"], vllm);
}

#[tokio::test]
async fn request_settings_are_not_part_of_the_run_contract() {
    let url = "http://127.0.0.1:9/v1";
    let plain = config(json!({"id": "vllm", "base_url": url}));
    let tuned = config(json!({
        "id": "vllm", "base_url": url,
        "request": {"default_headers": {"x-gateway-token": "tok-gateway"},
                    "extra_body": {"top_k": 5}, "timeout_ms": 1500},
        "chat": {"parallel_tool_calls": false},
    }));
    let plain = plain.open().await.unwrap();
    let tuned = tuned.open().await.unwrap();
    assert_eq!(plain.agent().contract(), tuned.agent().contract());
}

#[tokio::test]
async fn a_refused_header_never_reaches_the_open_error_text() {
    let config = config(json!({
        "id": "vllm", "base_url": "http://127.0.0.1:9/v1",
        "request": {"default_headers": {"Host": "tok-never-shown"}},
    }));
    let Err(error) = config.open().await else {
        panic!("a host header is refused")
    };
    assert!(matches!(error, OpenError::Provider(_)));
    let text = format!("{error} {error:?}");
    assert!(!text.contains("tok-never-shown"), "{text}");
}
