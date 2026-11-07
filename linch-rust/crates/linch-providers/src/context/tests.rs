use linch_core::Provider;
use serde_json::json;

use crate::test_server::serve_once;
use crate::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ResponsesOptions, Server, ServerOptions,
};

fn key() -> ApiKey {
    ApiKey::new("sk-context-window-test").unwrap()
}

fn ok(body: &serde_json::Value) -> String {
    let body = body.to_string();
    format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    )
}

#[test]
fn the_hosted_adapters_know_python_s_windows() {
    let anthropic = Anthropic::new(AnthropicOptions::new(key())).unwrap();
    assert_eq!(anthropic.context_window("claude-sonnet-4-6"), 200_000);
    assert_eq!(anthropic.context_window("claude-next"), 200_000);
    let gemini = Gemini::new(GeminiOptions::new(key())).unwrap();
    assert_eq!(gemini.context_window("gemini-1.5-pro"), 2_000_000);
    assert_eq!(gemini.context_window("gemini-2.5-pro"), 1_048_576);
    assert_eq!(gemini.context_window("gemini-next"), 1_048_576);
    let responses = OpenAiResponses::new(ResponsesOptions::new(key())).unwrap();
    assert_eq!(responses.context_window("gpt-5"), 400_000);
    assert_eq!(responses.context_window("gpt-4.1"), 1_000_000);
    assert_eq!(responses.context_window("o1"), 200_000);
    assert_eq!(responses.context_window("gpt-next"), 128_000);
    let chat = OpenAiChat::new(OpenAiChatOptions::new(key())).unwrap();
    assert_eq!(chat.context_window("gpt-5.5"), 400_000);
    assert_eq!(chat.context_window("gpt-5.4"), 400_000);
    assert_eq!(chat.context_window("o1"), 200_000);
    assert_eq!(
        chat.context_window("gpt-4.1"),
        128_000,
        "not in the chat table"
    );
    let deepseek = DeepSeek::new(DeepSeekOptions::new(key())).unwrap();
    assert_eq!(deepseek.context_window("deepseek-chat"), 128_000);
}

#[test]
fn a_configured_window_wins() {
    let mut options = OpenAiChatOptions::new(key());
    options.context_window = Some(64_000);
    let chat = OpenAiChat::new(options).unwrap();
    assert_eq!(chat.context_window("gpt-5.5"), 64_000);
    let mut options = DeepSeekOptions::new(key());
    options.context_window = Some(1_000_000);
    let deepseek = DeepSeek::new(options).unwrap();
    assert_eq!(deepseek.context_window("deepseek-chat"), 1_000_000);
    let mut options = ServerOptions::new(Server::Vllm, "http://127.0.0.1:9/v1");
    options.context_window = Some(32_768);
    let vllm = OpenAiChat::server(options).unwrap();
    assert_eq!(vllm.context_window("qwen"), 32_768);
}

#[tokio::test]
async fn a_configured_server_window_skips_the_probe() {
    // Nothing listens on the port: a probe would fail, not hang.
    let mut options = ServerOptions::new(Server::LlamaCpp, "http://127.0.0.1:9/v1");
    options.context_window = Some(8192);
    let server = OpenAiChat::server(options).unwrap();
    server.prepare().await;
    assert_eq!(server.context_window("any"), 8192);
}

#[tokio::test]
async fn vllm_and_sglang_read_max_model_len_from_the_models_list() {
    for kind in [Server::Vllm, Server::SgLang] {
        let models = json!({"object": "list", "data": [
            {"id": "qwen", "object": "model", "max_model_len": 32_768},
            {"id": "llama", "object": "model", "max_model_len": 0},
            {"id": "mistral", "object": "model"},
        ]});
        let (url, request) = serve_once(ok(&models));
        let mut options = ServerOptions::new(kind, format!("{url}/v1"));
        options.api_key = Some(key());
        let server = OpenAiChat::server(options).unwrap();
        assert_eq!(server.context_window("qwen"), 128_000, "before the probe");
        server.prepare().await;
        let raw = request.join().unwrap();
        assert!(raw.starts_with("GET /v1/models HTTP/1.1"), "{raw}");
        assert!(raw.contains("authorization: Bearer sk-context-window-test"));
        assert_eq!(server.context_window("qwen"), 32_768);
        assert_eq!(server.context_window("llama"), 128_000, "no positive value");
        assert_eq!(server.context_window("other"), 128_000);
    }
}

#[tokio::test]
async fn a_server_with_one_model_answers_for_any_name() {
    let models = json!({"data": [{"id": "/models/qwen", "max_model_len": 40_960}]});
    let (url, _request) = serve_once(ok(&models));
    let server = OpenAiChat::server(ServerOptions::new(Server::Vllm, format!("{url}/v1"))).unwrap();
    server.prepare().await;
    assert_eq!(server.context_window("qwen"), 40_960);
}

#[tokio::test]
async fn llamacpp_reads_n_ctx_from_props() {
    let props = json!({"default_generation_settings": {"n_ctx": 16_384}, "total_slots": 1});
    let (url, request) = serve_once(ok(&props));
    let options = ServerOptions::new(Server::LlamaCpp, format!("{url}/v1"));
    let server = OpenAiChat::server(options).unwrap();
    server.prepare().await;
    let raw = request.join().unwrap();
    assert!(raw.starts_with("GET /v1/props HTTP/1.1"), "{raw}");
    assert_eq!(server.context_window("any"), 16_384);
    let top_level = json!({"n_ctx": 4096});
    let (url, _request) = serve_once(ok(&top_level));
    let server = OpenAiChat::server(ServerOptions::new(Server::LlamaCpp, url)).unwrap();
    server.prepare().await;
    assert_eq!(server.context_window("any"), 4096);
}

#[test]
fn the_props_urls_try_the_base_then_the_root() {
    assert_eq!(
        super::props_urls("http://h:8080/v1/"),
        ["http://h:8080/v1/props", "http://h:8080/props"]
    );
    assert_eq!(
        super::props_urls("http://h:8080"),
        ["http://h:8080/props", "http://h:8080/v1/props"]
    );
}

#[tokio::test]
async fn a_failed_probe_keeps_the_default() {
    let (url, _request) = serve_once(
        "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n".into(),
    );
    let server = OpenAiChat::server(ServerOptions::new(Server::Vllm, format!("{url}/v1"))).unwrap();
    server.prepare().await;
    assert_eq!(server.context_window("qwen"), 128_000);
    let openai = OpenAiChat::new(OpenAiChatOptions::new(key())).unwrap();
    openai.prepare().await;
    assert_eq!(
        openai.context_window("gpt-5.5"),
        400_000,
        "OpenAI is never probed"
    );
}
