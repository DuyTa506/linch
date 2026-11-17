//! Live vLLM model list and capabilities. Skips when `LINCH_TEST_VLLM_URL`
//! (the server's `/v1` URL) is unset; `LINCH_TEST_VLLM_MODEL` names the
//! model it serves.
//!
//! `LINCH_TEST_VLLM_URL=http://127.0.0.1:8201/v1 LINCH_TEST_VLLM_MODEL=qwen3.6-35b-a3b \
//!  cargo test -p linch-providers --test live_vllm -- --nocapture`

use linch_core::{ContextSource, Provider};
use linch_providers::{OpenAiChat, Server, ServerOptions};

fn vllm(test: &str) -> Option<(OpenAiChat, String)> {
    let Ok(url) = std::env::var("LINCH_TEST_VLLM_URL") else {
        eprintln!("{test}: skipped, LINCH_TEST_VLLM_URL is not set");
        return None;
    };
    let model = std::env::var("LINCH_TEST_VLLM_MODEL").unwrap_or_else(|_| "qwen3.6-35b-a3b".into());
    let options = ServerOptions::new(Server::Vllm, url);
    Some((
        OpenAiChat::server(options).expect("HTTP client builds"),
        model,
    ))
}

#[tokio::test]
async fn a_live_server_lists_its_model_with_the_window() {
    let Some((provider, model)) = vllm("a_live_server_lists_its_model_with_the_window") else {
        return;
    };
    let models = provider.list_models().await.expect("the list");
    eprintln!("list: {models:?}");
    let info = provider.model_info(&model).await.expect("the model");
    eprintln!("info: {info:?}");
    assert_eq!(info.id, model);
    assert!(info.context_window.is_some_and(|window| window > 0));
}

#[tokio::test]
async fn a_live_servers_capabilities_name_its_window_as_the_servers() {
    let Some((provider, model)) = vllm("a_live_servers_capabilities_name_its_window") else {
        return;
    };
    provider.prepare().await;
    let caps = provider.capabilities(&model);
    eprintln!("capabilities: {}", serde_json::to_string(&caps).unwrap());
    assert_eq!(caps.context_source, ContextSource::Server);
    let info = provider.model_info(&model).await.expect("the model");
    assert_eq!(Some(caps.context_window), info.context_window);
    assert!(!caps.native_output_schema && caps.declared);
}
