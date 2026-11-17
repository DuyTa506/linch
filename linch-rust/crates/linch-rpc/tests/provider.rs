//! `provider.capabilities`, `provider.models` and `provider.model`: they
//! take a provider config, answer before an agent is open, and fail with
//! typed errors that name neither the key nor the provider's URL.

mod common;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
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

/// Like [`serve`], but without a path on the URL it returns, and with the
/// head of every request it got.
fn serve_recording(body: Value) -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let body = body.to_string();
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    );
    let seen = Arc::new(Mutex::new(Vec::new()));
    let heads = Arc::clone(&seen);
    thread::spawn(move || {
        for socket in listener.incoming() {
            let Ok(mut socket) = socket else { return };
            let mut buffer = [0; 4096];
            let read = socket.read(&mut buffer).unwrap_or(0);
            let head = String::from_utf8_lossy(&buffer[..read]).into_owned();
            heads.lock().unwrap().push(head);
            let _ = socket.write_all(response.as_bytes());
        }
    });
    (origin, seen)
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
    // The vendor's own words never reach the client.
    assert!(!error.message.contains("rejected"), "{}", error.message);
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
    // A refused connection and a reset or a timeout look the same.
    assert_eq!(error.data, Some(json!({"kind": "unreachable"})));
    assert_eq!(error.message, "the provider could not be reached");
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
    // The server takes a `response_format`, but not with tools in the same
    // request: the run offers its final tool instead.
    assert_eq!(caps["output_schema"], "final_tool");
    assert!(caps.get("native_output_schema").is_none());
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

fn open_config(provider: Value) -> Value {
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    config["provider"] = provider;
    config
}

/// Every way a tenant allowed `provider_info` might aim the key at another
/// path of an allowed origin, or at another host, is refused for each
/// provider call and for `agent.open`, before anything is sent.
#[tokio::test]
async fn a_base_url_that_steers_the_path_is_refused_before_anything_is_sent() {
    let (origin, seen) = serve_recording(models());
    let allow = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(std::slice::from_ref(&origin))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", allow));
    let hostile = [
        format!("{origin}/v1/files#"),
        format!("{origin}/v1/files?"),
        format!("{origin}/v1?x=1"),
        format!("{origin}/v1/../files"),
        format!("{origin}/v1/./files"),
        format!("{origin}/v1/%2e%2e/files"),
        format!("{origin}/v1/%2E%2E/files"),
        format!("{origin}/v1%2ffiles"),
        format!("{origin}//v1"),
        format!("{origin}/v1//"),
        format!("{origin}/v1\\..\\files"),
        origin.replace("http://", "http://user:pw@"),
        origin.replace("http://", "http://127.0.0.1:9@"),
        // A plain path that is not the provider's own.
        format!("{origin}/v1/files"),
        format!("{origin}/other/v1"),
    ];
    for url in &hostile {
        let provider = vllm(url);
        let expected = (code::NOT_ALLOWED, Some("provider_base_url".to_owned()));
        for (method, params) in [
            (method::PROVIDER_MODELS, json!({"provider": provider})),
            (
                method::PROVIDER_MODEL,
                json!({"provider": provider, "model": "qwen"}),
            ),
            (
                method::PROVIDER_CAPABILITIES,
                json!({"provider": provider, "model": "qwen"}),
            ),
        ] {
            let error = rpc.call(method, params).await.unwrap_err();
            assert_eq!(refusal(error), expected, "{method} {url:?}");
        }
        let kind = refused(&rpc, open_config(provider)).await;
        assert_eq!(kind.as_deref(), Some("provider_base_url"), "open {url:?}");
    }
    assert!(
        seen.lock().unwrap().is_empty(),
        "nothing reached the server: {:?}",
        seen.lock().unwrap()
    );
    // The provider's own paths on that origin pass.
    for allowed in [
        format!("{origin}/v1"),
        format!("{origin}/v1/"),
        origin.clone(),
    ] {
        let listed = rpc
            .call(method::PROVIDER_MODELS, json!({"provider": vllm(&allowed)}))
            .await
            .unwrap();
        assert_eq!(listed["models"][0]["id"], "qwen", "{allowed}");
    }
    let heads = seen.lock().unwrap().clone();
    assert!(
        heads[0].starts_with("GET /v1/models HTTP/1.1"),
        "{}",
        heads[0]
    );
    assert!(heads[2].starts_with("GET /models HTTP/1.1"), "{}", heads[2]);
}

/// A path the operator lists in full is the one path allowed on that
/// origin, for any provider.
#[tokio::test]
async fn an_operator_listed_prefix_is_the_one_path_allowed_there() {
    let (origin, seen) = serve_recording(models());
    let listed = format!("{origin}/openai/v1");
    let allow = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(std::slice::from_ref(&listed))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", allow));
    for allowed in [listed.clone(), format!("{listed}/")] {
        let models = rpc
            .call(method::PROVIDER_MODELS, json!({"provider": vllm(&allowed)}))
            .await
            .unwrap();
        assert_eq!(models["models"][0]["id"], "qwen", "{allowed}");
    }
    for other in [
        format!("{origin}/v1"),
        origin.clone(),
        format!("{origin}/openai/v2"),
        format!("{origin}/openai/v1/x"),
    ] {
        let error = rpc
            .call(method::PROVIDER_MODELS, json!({"provider": vllm(&other)}))
            .await
            .unwrap_err();
        assert_eq!(
            refusal(error),
            (code::NOT_ALLOWED, Some("provider_base_url".into())),
            "{other}"
        );
    }
    let heads = seen.lock().unwrap().clone();
    assert_eq!(heads.len(), 2, "{heads:?}");
    assert!(
        heads
            .iter()
            .all(|h| h.starts_with("GET /openai/v1/models "))
    );
}

/// A hosted provider's default path is its adapter's: OpenAI's `/v1`, and
/// none for DeepSeek, whose adapter adds what it needs.
#[tokio::test]
async fn a_hosted_providers_default_path_is_its_adapters_own() {
    let allow = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(&["https://gateway.example".to_owned()])
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", allow));
    let config = |id: &str, url: &str| open_config(json!({"id": id, "base_url": url}));
    for (id, url) in [
        ("openai-chat", "https://gateway.example"),
        ("openai-chat", "https://gateway.example/v2"),
        ("deepseek", "https://gateway.example/v1"),
        ("anthropic", "https://gateway.example/v1"),
        ("gemini", "https://gateway.example/v1beta"),
    ] {
        let kind = refused(&rpc, config(id, url)).await;
        assert_eq!(kind.as_deref(), Some("provider_base_url"), "{id} {url}");
    }
    // The paths that are theirs pass admission; the open then fails on the
    // missing key, which is not a refusal.
    for (id, url) in [
        ("openai-chat", "https://gateway.example/v1"),
        ("openai-responses", "https://gateway.example/v1/"),
        ("deepseek", "https://gateway.example"),
        ("anthropic", "https://gateway.example/"),
        ("gemini", "https://gateway.example"),
    ] {
        let kind = refused(&rpc, config(id, url)).await;
        assert_eq!(kind, None, "{id} {url}");
    }
}

/// `provider.models` and `provider.model` spend the server's key on
/// several requests each, so a tenant gets ten a minute between them; the
/// capabilities question and a local connection are not counted.
#[tokio::test]
async fn a_tenants_model_questions_are_rate_limited() {
    let (origin, seen) = serve_recording(models());
    let allow = Allowlists::new()
        .provider_info(true)
        .provider_base_urls(std::slice::from_ref(&origin))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", allow));
    let url = format!("{origin}/v1");
    let params = json!({"provider": vllm(&url)});
    let one = json!({"provider": vllm(&url), "model": "qwen"});
    for turn in 0..10 {
        let (method, params) = match turn % 2 {
            0 => (method::PROVIDER_MODELS, params.clone()),
            _ => (method::PROVIDER_MODEL, one.clone()),
        };
        rpc.call(method, params).await.unwrap();
    }
    let before = seen.lock().unwrap().len();
    for (method, params) in [
        (method::PROVIDER_MODELS, params.clone()),
        (method::PROVIDER_MODEL, one.clone()),
    ] {
        let error = rpc.call(method, params).await.unwrap_err();
        let ClientError::Rpc(error) = error else {
            panic!("{error:?}")
        };
        assert_eq!(error.code, code::LIMIT_REACHED);
        let data = error.data.unwrap();
        assert_eq!(data["kind"], "provider_calls");
        assert_eq!(data["max"], 10);
    }
    assert_eq!(seen.lock().unwrap().len(), before, "refused before sending");
    rpc.call(method::PROVIDER_CAPABILITIES, one).await.unwrap();
    let local = Rpc::new();
    for _ in 0..12 {
        local
            .call(method::PROVIDER_MODELS, params.clone())
            .await
            .unwrap();
    }
}

/// A tenant's request headers ride the capabilities probe (they are how a
/// gateway is reached), and the probe still carries no key: a tenant cannot
/// set a key header, and the provider is built without the server's.
#[tokio::test]
async fn the_capabilities_probe_sends_the_request_headers_and_no_key() {
    let (origin, seen) = serve_recording(models());
    let allow = Allowlists::new()
        .provider_info(true)
        .provider_request(true)
        .provider_base_urls(std::slice::from_ref(&origin))
        .unwrap();
    let rpc = Rpc::remote(tenant("acme", allow));
    let url = format!("{origin}/v1");
    let mut provider = vllm(&url);
    provider["request"] = json!({"default_headers": {"x-gateway-token": "tok-gw"}});
    let caps = rpc
        .call(
            method::PROVIDER_CAPABILITIES,
            json!({"provider": provider, "model": "qwen"}),
        )
        .await
        .unwrap();
    assert_eq!(caps["context_source"], "server");
    let heads = seen.lock().unwrap().join("\n").to_ascii_lowercase();
    assert!(heads.contains("x-gateway-token: tok-gw"), "{heads}");
    assert!(!heads.contains("authorization"), "{heads}");

    // A key header is not the tenant's to set, so nothing is sent for it.
    let before = seen.lock().unwrap().len();
    provider["request"] = json!({"default_headers": {"Authorization": "Bearer tok-gw"}});
    let error = rpc
        .call(
            method::PROVIDER_CAPABILITIES,
            json!({"provider": provider, "model": "qwen"}),
        )
        .await
        .unwrap_err();
    assert_eq!(
        refusal(error),
        (code::NOT_ALLOWED, Some("provider_request".into()))
    );
    assert_eq!(seen.lock().unwrap().len(), before);
}
