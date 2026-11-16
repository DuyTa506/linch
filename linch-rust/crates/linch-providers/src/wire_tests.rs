//! What the request options put on the wire, for every adapter: the raw
//! request a local server reads.

use std::io::{BufRead, BufReader, Read};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use linch_core::{Provider, ProviderError, ProviderRequest};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use crate::openai::test_support::{error_response, request, serve_once, weather_tool};
use crate::{
    Anthropic, AnthropicOptions, ApiKey, ChatOptions, DeepSeek, DeepSeekOptions, Gemini,
    GeminiOptions, OpenAiChat, OpenAiChatOptions, OpenAiResponses, RequestOptions,
    ResponsesOptions, Server, ServerOptions,
};

fn key() -> ApiKey {
    ApiKey::new("sk-test-key").unwrap()
}

fn options() -> RequestOptions {
    let mut options = RequestOptions::default()
        .header("x-gateway-token", "gw-secret")
        .unwrap();
    options.extra_body = json!({"top_k": 40, "temperature": 9.0})
        .as_object()
        .cloned()
        .unwrap();
    options
}

/// Send `request` and return the raw request the server read.
async fn sent(
    provider: &dyn Provider,
    url_handle: thread::JoinHandle<String>,
    req: ProviderRequest,
) -> (String, Value) {
    let result = provider.stream(req, CancellationToken::new()).await;
    assert!(
        matches!(result, Err(ProviderError::Status { status: 400, .. })),
        "the canned 400 is the answer"
    );
    let raw = url_handle.join().unwrap();
    let (head, body) = raw.split_once("\r\n\r\n").expect("head and body");
    (
        head.to_ascii_lowercase(),
        serde_json::from_str(body).unwrap(),
    )
}

fn bad_request() -> String {
    error_response("400 Bad Request", "", "{}")
}

fn assert_options_sent(head: &str, body: &Value) {
    assert!(head.contains("x-gateway-token: gw-secret"), "{head}");
    assert_eq!(body["top_k"], 40);
    assert_ne!(
        body["temperature"], 9.0,
        "the request's own temperature wins"
    );
}

fn with_temperature() -> ProviderRequest {
    ProviderRequest {
        temperature: Some(0.25),
        ..request("m")
    }
}

#[tokio::test]
async fn every_adapter_sends_the_extra_body_and_headers() {
    let (url, handle) = serve_once(bad_request());
    let mut o = OpenAiChatOptions::new(key());
    o.base_url = url;
    o.request = options();
    let (head, body) = sent(&OpenAiChat::new(o).unwrap(), handle, with_temperature()).await;
    assert_options_sent(&head, &body);
    assert!(
        head.contains("authorization: bearer"),
        "the key header stays: {head}"
    );
    assert_eq!(body["temperature"], 0.25);

    let (url, handle) = serve_once(bad_request());
    let mut o = DeepSeekOptions::new(key());
    o.base_url = url;
    o.request = options();
    let (head, body) = sent(&DeepSeek::new(o).unwrap(), handle, with_temperature()).await;
    assert_options_sent(&head, &body);

    let (url, handle) = serve_once(bad_request());
    let mut o = ResponsesOptions::new(key());
    o.base_url = url;
    o.request = options();
    let (head, body) = sent(
        &OpenAiResponses::new(o).unwrap(),
        handle,
        with_temperature(),
    )
    .await;
    assert_options_sent(&head, &body);

    let (url, handle) = serve_once(bad_request());
    let mut o = AnthropicOptions::new(key());
    o.base_url = url;
    o.request = options();
    let (head, body) = sent(&Anthropic::new(o).unwrap(), handle, with_temperature()).await;
    assert_options_sent(&head, &body);
    assert!(head.contains("x-api-key:"), "{head}");

    let (url, handle) = serve_once(bad_request());
    let mut o = GeminiOptions::new(key());
    o.base_url = url;
    o.request = options();
    let (head, body) = sent(&Gemini::new(o).unwrap(), handle, with_temperature()).await;
    assert_options_sent(&head, &body);
}

#[tokio::test]
async fn chat_options_reach_the_body_of_a_server() {
    let (url, handle) = serve_once(bad_request());
    let mut o = ServerOptions::new(Server::Vllm, url);
    o.chat = ChatOptions {
        parallel_tool_calls: Some(false),
        include_stream_options: false,
        chat_template_kwargs: json!({"preserve_thinking": true})
            .as_object()
            .cloned()
            .unwrap(),
        sampling_params: json!({"top_k": 20, "min_p": 0.1})
            .as_object()
            .cloned()
            .unwrap(),
    };
    o.request.extra_body = json!({"top_k": 50, "cache_salt": "tenant-1"})
        .as_object()
        .cloned()
        .unwrap();
    let req = ProviderRequest {
        tools: vec![weather_tool()],
        ..request("m")
    };
    let (_, body) = sent(&OpenAiChat::server(o).unwrap(), handle, req).await;
    assert_eq!(body["parallel_tool_calls"], false);
    assert!(body.get("stream_options").is_none(), "{body}");
    assert_eq!(body["top_k"], 50, "extra_body wins over sampling_params");
    assert_eq!(body["min_p"], 0.1);
    assert_eq!(body["cache_salt"], "tenant-1");
    assert_eq!(
        body["chat_template_kwargs"],
        json!({"preserve_thinking": true})
    );
}

#[tokio::test]
async fn without_options_the_body_is_what_it_was() {
    let (url, handle) = serve_once(bad_request());
    let mut o = OpenAiChatOptions::new(key());
    o.base_url = url;
    let req = ProviderRequest {
        tools: vec![weather_tool()],
        ..request("m")
    };
    let (head, body) = sent(&OpenAiChat::new(o).unwrap(), handle, req).await;
    assert!(!head.contains("x-gateway"), "{head}");
    assert_eq!(body["stream_options"], json!({"include_usage": true}));
    assert!(body.get("parallel_tool_calls").is_none());
}

#[tokio::test]
async fn a_timeout_ends_a_call_that_the_server_never_answers() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    thread::spawn(move || {
        let (socket, _) = listener.accept().unwrap();
        let mut reader = BufReader::new(socket);
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = v.trim().parse().unwrap();
            }
            if line == "\r\n" {
                break;
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        thread::sleep(Duration::from_secs(5));
    });
    let mut o = OpenAiChatOptions::new(key());
    o.base_url = url;
    o.request.timeout = Some(Duration::from_millis(300));
    let started = std::time::Instant::now();
    let result = OpenAiChat::new(o)
        .unwrap()
        .stream(request("m"), CancellationToken::new())
        .await;
    assert!(matches!(result, Err(ProviderError::Transport { .. })));
    assert!(started.elapsed() < Duration::from_secs(4));
}

#[tokio::test]
async fn a_key_header_from_the_caller_replaces_the_provider_key() {
    let (url, handle) = serve_once(bad_request());
    let mut o = OpenAiChatOptions::new(key());
    o.base_url = url;
    o.request = RequestOptions::default()
        .header("Authorization", "Bearer gw-token")
        .unwrap();
    let (head, _) = sent(&OpenAiChat::new(o).unwrap(), handle, request("m")).await;
    assert_eq!(head.matches("authorization:").count(), 1, "{head}");
    assert!(
        head.contains("bearer gw-token") && !head.contains("sk-test-key"),
        "{head}"
    );

    let (url, handle) = serve_once(bad_request());
    let mut o = AnthropicOptions::new(key());
    o.base_url = url;
    o.request = RequestOptions::default()
        .header("x-api-key", "gw-token")
        .unwrap();
    let (head, _) = sent(&Anthropic::new(o).unwrap(), handle, request("m")).await;
    assert_eq!(head.matches("x-api-key:").count(), 1, "{head}");
    assert!(
        head.contains("gw-token") && !head.contains("sk-test-key"),
        "{head}"
    );

    let (url, handle) = serve_once(bad_request());
    let mut o = GeminiOptions::new(key());
    o.base_url = url;
    o.request = RequestOptions::default()
        .header("x-goog-api-key", "gw-token")
        .unwrap();
    let (head, _) = sent(&Gemini::new(o).unwrap(), handle, request("m")).await;
    assert_eq!(head.matches("x-goog-api-key:").count(), 1, "{head}");
    assert!(!head.contains("sk-test-key"), "{head}");
}
