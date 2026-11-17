//! `provider.capabilities`, `provider.models` and `provider.model`: they
//! take a provider config, answer before an agent is open, and fail with
//! typed errors that name neither the key nor the provider's URL.

mod common;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

use common::*;
use linch_rpc::protocol::method;
use linch_rpc::{Allowlists, ClientError, code};
use serde_json::{Value, json};

/// A server answering every request with `status` and `body`, until the
/// test ends. Returns its base URL (with `/v1`).
fn serve(status: &str, body: Value) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/v1", listener.local_addr().unwrap());
    let body = body.to_string();
    let response = format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    );
    thread::spawn(move || {
        for socket in listener.incoming() {
            let Ok(mut socket) = socket else { return };
            let mut buffer = [0; 4096];
            let _ = socket.read(&mut buffer);
            let _ = socket.write_all(response.as_bytes());
        }
    });
    url
}

fn models() -> Value {
    json!({"object": "list", "data": [
        {"id": "qwen", "object": "model", "max_model_len": 262_144},
        {"id": "small", "object": "model", "max_model_len": 8192}]})
}

fn vllm(url: &str) -> Value {
    json!({"id": "vllm", "base_url": url})
}

#[tokio::test]
async fn a_client_lists_a_providers_models_before_it_opens_an_agent() {
    let url = serve("200 OK", models());
    let rpc = Rpc::new();

    let listed = rpc
        .call(method::PROVIDER_MODELS, json!({"provider": vllm(&url)}))
        .await
        .unwrap();
    assert_eq!(
        listed,
        json!({"models": [
            {"id": "qwen", "context_window": 262_144},
            {"id": "small", "context_window": 8192}]})
    );
    let one = rpc
        .call(
            method::PROVIDER_MODEL,
            json!({"provider": vllm(&url), "model": "small"}),
        )
        .await
        .unwrap();
    assert_eq!(one, json!({"id": "small", "context_window": 8192}));
}

#[tokio::test]
async fn a_model_the_list_lacks_and_a_provider_with_no_list_are_typed_errors() {
    let url = serve("200 OK", models());
    let rpc = Rpc::new();

    let missing = rpc
        .call(
            method::PROVIDER_MODEL,
            json!({"provider": vllm(&url), "model": "nope"}),
        )
        .await
        .unwrap_err();
    let ClientError::Rpc(missing) = missing else {
        panic!("{missing:?}")
    };
    assert_eq!(missing.code, code::MODEL_NOT_FOUND);
    assert_eq!(missing.data, Some(json!({"model": "nope"})));

    let scripted = json!({"id": "scripted", "turns": []});
    let error = rpc
        .call(method::PROVIDER_MODELS, json!({"provider": scripted}))
        .await
        .unwrap_err();
    let ClientError::Rpc(error) = error else {
        panic!("{error:?}")
    };
    assert_eq!(error.code, code::MODELS_UNSUPPORTED);
    assert_eq!(error.data, Some(json!({"provider": "scripted"})));
}

#[tokio::test]
async fn a_failure_is_a_typed_error_that_leaks_neither_key_nor_url() {
    let secret = "sk-rpc-provider-secret-77";
    let body = json!({"error": {"message": format!("rejected {secret}")}});
    let url = serve("401 Unauthorized", body);
    let rpc = Rpc::new();

    let error = rpc
        .call(method::PROVIDER_MODELS, json!({"provider": vllm(&url)}))
        .await
        .unwrap_err();
    let ClientError::Rpc(error) = error else {
        panic!("{error:?}")
    };
    assert_eq!(error.code, code::PROVIDER_REQUEST_FAILED);
    assert_eq!(error.data, Some(json!({"kind": "status", "status": 401})));
    let host = url.trim_start_matches("http://").trim_end_matches("/v1");
    let text = serde_json::to_string(&error).unwrap();
    assert!(!text.contains(host) && !text.contains("/models"), "{text}");

    // Nothing listens: a connect failure, again without the address.
    let closed = {
        let socket = TcpListener::bind("127.0.0.1:0").unwrap();
        format!("http://{}/v1", socket.local_addr().unwrap())
    };
    let error = rpc
        .call(method::PROVIDER_MODELS, json!({"provider": vllm(&closed)}))
        .await
        .unwrap_err();
    let ClientError::Rpc(error) = error else {
        panic!("{error:?}")
    };
    assert_eq!(error.code, code::PROVIDER_REQUEST_FAILED);
    assert_eq!(error.data, Some(json!({"kind": "connect"})));
    let host = closed.trim_start_matches("http://").trim_end_matches("/v1");
    let text = serde_json::to_string(&error).unwrap();
    assert!(!text.contains(host) && !text.contains("/models"), "{text}");
}

#[tokio::test]
async fn capabilities_come_from_the_adapter_and_the_servers_window() {
    let url = serve("200 OK", models());
    let rpc = Rpc::new();

    let caps = rpc
        .call(
            method::PROVIDER_CAPABILITIES,
            json!({"provider": vllm(&url), "model": "qwen"}),
        )
        .await
        .unwrap();
    assert_eq!(caps["provider"], "vllm");
    assert_eq!(caps["declared"], true);
    assert_eq!(caps["context_window"], 262_144);
    assert_eq!(caps["context_source"], "server");
    assert_eq!(caps["native_output_schema"], false);
    assert_eq!(caps["tool_choice"]["named"], true);
    assert_eq!(caps["thinking"]["disable"], true);
    assert_eq!(caps["vision"], "model_dependent");

    // A provider that needs a key answers without one.
    let anthropic = rpc
        .call(
            method::PROVIDER_CAPABILITIES,
            json!({"provider": {"id": "anthropic"}, "model": "claude-sonnet-4-6"}),
        )
        .await
        .unwrap();
    assert_eq!(anthropic["prompt_cache"], "explicit");
    assert_eq!(
        anthropic["tool_choice"]["forced_refused_with"],
        json!(["budgeted"])
    );
}

#[tokio::test]
async fn a_tenant_asks_only_when_allowed_and_only_of_its_own_endpoints() {
    let url = serve("200 OK", models());
    let params = json!({"provider": vllm(&url)});

    // Off by default.
    let denied = Rpc::remote(tenant("acme", Allowlists::new()));
    let error = denied
        .call(method::PROVIDER_MODELS, params.clone())
        .await
        .unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("provider_info".into()))
    );
    let error = denied
        .call(
            method::PROVIDER_CAPABILITIES,
            json!({"provider": vllm(&url), "model": "qwen"}),
        )
        .await
        .unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("provider_info".into()))
    );

    // Allowed to ask, but not of this endpoint: the key stays home.
    let elsewhere = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(&["http://example.invalid".to_owned()])
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", elsewhere));
    let error = rpc
        .call(method::PROVIDER_MODELS, params.clone())
        .await
        .unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("provider_base_url".into()))
    );

    // Allowed to ask and the endpoint is listed.
    let listed = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(std::slice::from_ref(&url))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", listed));
    let models = rpc.call(method::PROVIDER_MODELS, params).await.unwrap();
    assert_eq!(models["models"][0]["id"], "qwen");
}
