use linch_core::{ModelInfo, ModelsError, Provider, ProviderError, ScriptedProvider};
use serde_json::json;

use crate::test_server::{closed_url, redirect_response, serve_sequence};
use crate::{
    Anthropic, AnthropicOptions, ApiKey, DeepSeek, DeepSeekOptions, Gemini, GeminiOptions,
    OpenAiChat, OpenAiChatOptions, OpenAiResponses, ResponsesOptions, Server, ServerOptions,
};

const SECRET: &str = "sk-model-list-secret-91c4";

fn key() -> ApiKey {
    ApiKey::new(SECRET).unwrap()
}

fn answer(status: &str, body: &serde_json::Value) -> String {
    let body = body.to_string();
    format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    )
}

fn ok(body: &serde_json::Value) -> String {
    answer("200 OK", body)
}

fn provider_error(error: ModelsError) -> ProviderError {
    match error {
        ModelsError::Provider(error) => error,
        other => panic!("{other:?}"),
    }
}

/// No part of the server's address, path or the key is in `error`.
fn assert_no_leak(error: &ProviderError, url: &str) {
    let host = url.trim_start_matches("http://");
    for text in [error.to_string(), format!("{error:?}")] {
        for secret in [SECRET, host, "127.0.0.1", "/models", "http://"] {
            assert!(!text.contains(secret), "{secret:?} in {text}");
        }
    }
}

#[tokio::test]
async fn a_vllm_list_gives_each_models_window() {
    let body = json!({"object": "list", "data": [
        {"id": "qwen", "object": "model", "max_model_len": 262_144},
        {"id": "other", "object": "model"},
    ]});
    let (url, server) = serve_sequence(vec![ok(&body), ok(&body), ok(&body)]);
    let options = ServerOptions::new(Server::Vllm, format!("{url}/v1"));
    let provider = OpenAiChat::server(options).unwrap();

    let models = provider.list_models().await.unwrap().models;
    let mut qwen = ModelInfo::new("qwen");
    qwen.context_window = Some(262_144);
    assert_eq!(models, [qwen.clone(), ModelInfo::new("other")]);
    assert_eq!(provider.model_info("qwen").await.unwrap(), qwen);
    let missing = provider.model_info("nope").await.unwrap_err();
    assert!(
        matches!(missing, ModelsError::NotFound { model, truncated: false } if model == "nope")
    );
    let heads = server.join().unwrap();
    assert!(
        heads[0].starts_with("GET /v1/models HTTP/1.1"),
        "{}",
        heads[0]
    );
    assert!(
        !heads[0].contains("authorization"),
        "a keyless server gets no key"
    );
}

#[tokio::test]
async fn a_llamacpp_list_takes_its_window_from_props() {
    let models = json!({"data": [{"id": "gguf", "object": "model"}]});
    let props = json!({"default_generation_settings": {"n_ctx": 8192}});
    let (url, server) = serve_sequence(vec![ok(&models), ok(&props)]);
    let provider =
        OpenAiChat::server(ServerOptions::new(Server::LlamaCpp, format!("{url}/v1"))).unwrap();

    let listed = provider.list_models().await.unwrap().models;
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].context_window, Some(8192));
    let heads = server.join().unwrap();
    assert!(heads[1].starts_with("GET /v1/props"), "{}", heads[1]);
}

#[tokio::test]
async fn an_openai_list_has_ids_only_and_sends_the_key_as_a_bearer() {
    let body = json!({"object": "list", "data": [
        {"id": "gpt-5.5", "object": "model", "created": 1, "owned_by": "openai"}]});
    let (url, server) = serve_sequence(vec![ok(&body), ok(&body)]);
    let mut options = OpenAiChatOptions::new(key());
    options.base_url = format!("{url}/v1");
    let chat = OpenAiChat::new(options).unwrap();
    assert_eq!(
        chat.list_models().await.unwrap().models,
        [ModelInfo::new("gpt-5.5")]
    );
    let mut options = ResponsesOptions::new(key());
    options.base_url = format!("{url}/v1");
    let responses = OpenAiResponses::new(options).unwrap();
    assert_eq!(responses.model_info("gpt-5.5").await.unwrap().id, "gpt-5.5");
    let heads = server.join().unwrap();
    for head in heads {
        assert!(
            head.contains(&format!("authorization: Bearer {SECRET}")),
            "{head}"
        );
    }
}

#[tokio::test]
async fn a_deepseek_list_gives_window_output_and_modalities() {
    let body = json!({"object": "list", "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek",
         "context_window": 1_000_000, "max_output_tokens": 65_536,
         "input_modalities": ["text", "image"]},
        {"id": "deepseek-v4-pro", "object": "model", "input_modalities": ["text"]}]});
    let (url, server) = serve_sequence(vec![ok(&body)]);
    let mut options = DeepSeekOptions::new(key());
    options.base_url = url;
    let models = DeepSeek::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap()
        .models;
    server.join().unwrap();

    assert_eq!(models[0].context_window, Some(1_000_000));
    assert_eq!(models[0].max_output_tokens, Some(65_536));
    assert_eq!(models[0].input_modalities, ["text", "image"]);
    assert_eq!(models[1].input_modalities, ["text"]);
    assert_eq!(models[1].context_window, None);
}

#[tokio::test]
async fn an_anthropic_list_follows_its_pages() {
    let first = json!({"data": [{
        "id": "claude-a", "type": "model", "display_name": "Claude A",
        "max_input_tokens": 1_000_000, "max_tokens": 64_000,
        "capabilities": {"image_input": {"supported": true}}}],
        "has_more": true, "first_id": "claude-a", "last_id": "claude-a"});
    let second = json!({"data": [{
        "id": "claude-b", "type": "model", "display_name": "Claude B",
        "capabilities": {"image_input": {"supported": false}}}],
        "has_more": false, "first_id": "claude-b", "last_id": "claude-b"});
    let (url, server) = serve_sequence(vec![ok(&first), ok(&second)]);
    let mut options = AnthropicOptions::new(key());
    options.base_url = url;
    let provider = Anthropic::new(options).unwrap();

    let list = provider.list_models().await.unwrap();
    let models = list.models;
    assert!(!list.truncated, "a list read to its end is not cut");
    let heads = server.join().unwrap();
    assert_eq!(models.len(), 2);
    assert_eq!(models[0].display_name.as_deref(), Some("Claude A"));
    assert_eq!(models[0].context_window, Some(1_000_000));
    assert_eq!(models[0].max_output_tokens, Some(64_000));
    assert_eq!(models[0].input_modalities, ["text", "image"]);
    assert_eq!(models[1].input_modalities, ["text"]);
    assert!(
        heads[0].starts_with("GET /v1/models?limit=1000 "),
        "{}",
        heads[0]
    );
    assert!(heads[1].contains("after_id=claude%2Da"), "{}", heads[1]);
    for head in &heads {
        assert!(head.contains(&format!("x-api-key: {SECRET}")), "{head}");
        assert!(head.contains("anthropic-version: 2023-06-01"), "{head}");
    }
}

#[tokio::test]
async fn a_gemini_list_follows_its_pages_and_skips_embedding_models() {
    let first = json!({"models": [
        {"name": "models/gemini-3-pro", "displayName": "Gemini 3 Pro",
         "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
         "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/text-embedding", "supportedGenerationMethods": ["embedContent"]}],
        "nextPageToken": "tok/1"});
    let second = json!({"models": [{"name": "models/gemini-3-flash"}]});
    let (url, server) = serve_sequence(vec![ok(&first), ok(&second)]);
    let mut options = GeminiOptions::new(key());
    options.base_url = url;
    let provider = Gemini::new(options).unwrap();

    let models = provider.list_models().await.unwrap().models;
    let heads = server.join().unwrap();
    let ids: Vec<_> = models.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, ["gemini-3-pro", "gemini-3-flash"]);
    assert_eq!(models[0].display_name.as_deref(), Some("Gemini 3 Pro"));
    assert_eq!(models[0].context_window, Some(1_048_576));
    assert_eq!(models[0].max_output_tokens, Some(65_536));
    assert!(
        heads[0].starts_with("GET /v1beta/models?pageSize=1000 "),
        "{}",
        heads[0]
    );
    assert!(heads[1].contains("pageToken=tok%2F1"), "{}", heads[1]);
    assert!(
        heads[0].contains(&format!("x-goog-api-key: {SECRET}")),
        "{}",
        heads[0]
    );
    assert!(
        !heads[0].contains(&format!("key={SECRET}")),
        "the key is not in the URL"
    );
}

#[tokio::test]
async fn a_server_that_always_says_more_ends_after_a_few_pages() {
    let more = json!({"data": [{"id": "m"}], "has_more": true, "last_id": "m"});
    let pages = vec![ok(&more); super::MAX_PAGES];
    let (url, server) = serve_sequence(pages);
    let mut options = AnthropicOptions::new(key());
    options.base_url = url;
    let list = Anthropic::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap();
    assert_eq!(list.models.len(), super::MAX_PAGES);
    assert!(list.truncated, "the cut is said, not silent");
    server.join().unwrap();
}

#[tokio::test]
async fn a_refused_connection_names_neither_the_host_nor_the_key() {
    let url = closed_url();
    let mut options = AnthropicOptions::new(key());
    options.base_url = url.clone();
    let error = Anthropic::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap_err();
    let error = provider_error(error);

    assert!(matches!(error, ProviderError::Connect { .. }), "{error:?}");
    assert_no_leak(&error, &url);
}

#[tokio::test]
async fn an_error_answer_is_a_status_without_the_key_or_the_url() {
    let body = json!({"error": {"type": "authentication_error",
        "message": format!("bad key {SECRET} on http://10.0.0.7:9/v1/models")}});
    let (url, server) = serve_sequence(vec![answer("401 Unauthorized", &body)]);
    let mut options = DeepSeekOptions::new(key());
    options.base_url = url.clone();
    let error = DeepSeek::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap_err();
    server.join().unwrap();
    let error = provider_error(error);

    assert!(
        matches!(&error, ProviderError::Status { status: 401, .. }),
        "{error:?}"
    );
    for text in [error.to_string(), format!("{error:?}")] {
        assert!(!text.contains(SECRET), "{text}");
        assert!(!text.contains(url.trim_start_matches("http://")), "{text}");
    }
}

#[tokio::test]
async fn a_body_that_is_not_an_error_object_is_not_echoed() {
    let (url, server) = serve_sequence(vec![format!(
        "HTTP/1.1 502 Bad Gateway\r\ncontent-length: 40\r\nconnection: close\r\n\r\n{}",
        format!("{SECRET} upstream http://127.0.0.1:1/models")
    )]);
    let mut options = OpenAiChatOptions::new(key());
    options.base_url = url.clone();
    let error = OpenAiChat::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap_err();
    server.join().unwrap();
    let error = provider_error(error);

    assert!(
        matches!(&error, ProviderError::Status { status: 502, .. }),
        "{error:?}"
    );
    assert_no_leak(&error, &url);
}

#[tokio::test]
async fn a_redirect_is_refused_not_followed() {
    let (url, server) = serve_sequence(vec![redirect_response()]);
    let mut options = OpenAiChatOptions::new(key());
    options.base_url = url;
    let error = OpenAiChat::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap_err();
    server.join().unwrap();

    crate::test_server::assert_redirect_refused(&provider_error(error));
}

#[tokio::test]
async fn an_answer_that_is_not_json_is_a_transport_error() {
    let (url, server) = serve_sequence(vec![
        "HTTP/1.1 200 OK\r\ncontent-length: 8\r\nconnection: close\r\n\r\n<html>ok".to_string(),
    ]);
    let mut options = OpenAiChatOptions::new(key());
    options.base_url = url.clone();
    let error = OpenAiChat::new(options)
        .unwrap()
        .list_models()
        .await
        .unwrap_err();
    server.join().unwrap();
    let error = provider_error(error);

    assert!(
        matches!(&error, ProviderError::Transport { .. }),
        "{error:?}"
    );
    assert_no_leak(&error, &url);
}

#[tokio::test]
async fn a_provider_without_a_model_list_says_so() {
    let scripted = ScriptedProvider::new(Vec::<linch_core::ScriptedTurn>::new());
    let error = scripted.list_models().await.unwrap_err();
    assert!(matches!(error, ModelsError::Unsupported { provider } if provider == "scripted"));
}

/// An error body of 10 MB that stalls after 64 KiB must not hold the call:
/// only its first 8 KiB are read.
#[tokio::test]
async fn an_error_body_is_read_to_8_kib_not_to_the_end() {
    let url = crate::test_server::serve_stalled_error(r#"{"error":{"message":"busy"}}"#);
    let mut options = OpenAiChatOptions::new(key());
    options.base_url = url;
    let provider = OpenAiChat::new(options).unwrap();
    let asked = tokio::time::timeout(std::time::Duration::from_secs(5), provider.list_models());
    let error = asked.await.expect("the call returned").unwrap_err();
    let error = provider_error(error);
    assert!(matches!(error, ProviderError::Status { status: 400, .. }));
}

/// The vendor's words are untrusted text: ASCII only, no control
/// characters, at most 120 characters, and never the key.
#[tokio::test]
async fn a_vendor_error_message_is_ascii_printable_and_short() {
    let long = "x".repeat(400);
    let message = format!("bad \u{1b}[31mkey\u{0}\n {SECRET} caf\u{e9} {long}");
    let body = json!({"error": {"message": message}});
    let (url, server) = serve_sequence(vec![answer("401 Unauthorized", &body)]);
    let mut options = DeepSeekOptions::new(key());
    options.base_url = url;
    let provider = DeepSeek::new(options).unwrap();
    let error = provider.list_models().await.unwrap_err();
    server.join().unwrap();
    let ProviderError::Status { message, .. } = provider_error(error) else {
        panic!("not a status error");
    };
    assert!(message.chars().count() <= 120, "{}", message.len());
    let plain = |c: char| c.is_ascii_graphic() || c == ' ';
    assert!(message.chars().all(plain), "{message:?}");
    assert!(message.starts_with("bad [31mkey"), "{message:?}");
    assert!(!message.contains(SECRET));
}

/// A listed `display_name` reaches clients as text: control characters and
/// direction overrides are dropped and the length is capped.
#[tokio::test]
async fn a_display_name_is_cleaned_before_it_is_returned() {
    let name = format!("Evil\u{1b}[2J\u{202e}Name\n{}", "y".repeat(300));
    let page = json!({"data": [
        {"id": "a", "display_name": name},
        {"id": "b", "display_name": "\u{7}\u{200b}"}], "has_more": false});
    let (url, server) = serve_sequence(vec![ok(&page)]);
    let mut options = AnthropicOptions::new(key());
    options.base_url = url;
    let list = Anthropic::new(options).unwrap().list_models().await;
    server.join().unwrap();
    let list = list.unwrap();
    let shown = list.models[0].display_name.as_deref().unwrap();
    assert!(shown.starts_with("Evil[2JName"), "{shown:?}");
    assert!(shown.chars().count() <= 100, "{}", shown.chars().count());
    assert!(shown.chars().all(|c| !c.is_control()), "{shown:?}");
    assert_eq!(
        list.models[1].display_name, None,
        "nothing readable is left"
    );
}
