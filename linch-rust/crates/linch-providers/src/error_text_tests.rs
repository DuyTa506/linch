//! Error text a server sends, over a real HTTP exchange with each adapter:
//! secrets (the provider key, a header value, the cache salt) are removed
//! before the text is cut to 500 characters, so a secret that straddles
//! the cut leaves no part of itself behind, whether the error is an HTTP
//! error body or a frame inside a 200 stream.

use futures::StreamExt;
use linch_core::{Message, Provider, ProviderError, ProviderRequest};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use crate::request::MAX_ERROR_CHARS;
use crate::test_server::serve_once;
use crate::{
    Anthropic, AnthropicOptions, ApiKey, CacheSalt, DeepSeek, DeepSeekOptions, Gemini,
    GeminiOptions, OpenAiChat, OpenAiChatOptions, OpenAiResponses, RequestOptions,
    ResponsesOptions,
};

/// A credential a caller sends as a header, or a cache salt; 32 bytes.
const SECRET: &str = "gw-secret-value-0123456789abcdef";
/// A provider key; 32 bytes.
const KEY: &str = "sk-leaktest-0123456789abcdefghij";

type Build = fn(String, RequestOptions) -> Box<dyn Provider>;

struct Wire {
    name: &'static str,
    build: Build,
    /// An error frame inside a 200 stream, carrying `message`.
    stream: fn(&str) -> String,
    /// A non-2xx body, carrying `message`.
    http: fn(&str) -> (u16, String),
}

fn key() -> ApiKey {
    ApiKey::new(KEY).unwrap()
}

fn wires() -> Vec<Wire> {
    vec![
        Wire {
            name: "anthropic",
            build: |url, request| {
                let mut options = AnthropicOptions::new(key());
                options.base_url = url;
                options.request = request;
                Box::new(Anthropic::new(options).unwrap())
            },
            stream: |message| {
                let error =
                    json!({"type": "error", "error": {"type": "api_error", "message": message}});
                format!("event: error\ndata: {error}\n\n")
            },
            http: |message| {
                let error = json!({"type": "error", "error": {"type": "invalid_request_error", "message": message}});
                (400, error.to_string())
            },
        },
        Wire {
            name: "gemini",
            build: |url, request| {
                let mut options = GeminiOptions::new(key());
                options.base_url = url;
                options.request = request;
                Box::new(Gemini::new(options).unwrap())
            },
            stream: |message| {
                let error =
                    json!({"error": {"code": 500, "message": message, "status": "INTERNAL"}});
                format!("data: {error}\n\n")
            },
            http: |message| {
                let error = json!({"error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}});
                (400, error.to_string())
            },
        },
        Wire {
            name: "openai-chat",
            build: |url, request| {
                let mut options = OpenAiChatOptions::new(key());
                options.base_url = url;
                options.request = request;
                Box::new(OpenAiChat::new(options).unwrap())
            },
            stream: chat_stream,
            http: chat_http,
        },
        Wire {
            name: "deepseek",
            build: |url, request| {
                let mut options = DeepSeekOptions::new(key());
                options.base_url = url;
                options.request = request;
                Box::new(DeepSeek::new(options).unwrap())
            },
            stream: chat_stream,
            http: chat_http,
        },
        Wire {
            name: "openai-responses",
            build: |url, request| {
                let mut options = ResponsesOptions::new(key());
                options.base_url = url;
                options.request = request;
                Box::new(OpenAiResponses::new(options).unwrap())
            },
            stream: |message| {
                let error = json!({"type": "error", "code": "server_error", "message": message});
                format!("data: {error}\n\n")
            },
            http: chat_http,
        },
    ]
}

fn chat_stream(message: &str) -> String {
    let error = json!({"error": {"type": "server_error", "message": message}});
    format!("data: {error}\n\n")
}

fn chat_http(message: &str) -> (u16, String) {
    let error = json!({"error": {"type": "invalid_request_error", "message": message}});
    (400, error.to_string())
}

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "m".into(),
        system: vec![],
        tools: vec![],
        appended_tools: vec![],
        context_tail: 0,
        messages: vec![Message::user_text("hi")],
        max_output_tokens: None,
        temperature: None,
        stop_sequences: vec![],
        effort: None,
        thinking: None,
        cache_control: None,
        output_schema: None,
        tool_choice: None,
    }
}

fn message(err: &ProviderError) -> String {
    match err {
        ProviderError::Connect { message }
        | ProviderError::Transport { message }
        | ProviderError::Status { message, .. }
        | ProviderError::ContextLength { message, .. }
        | ProviderError::InvalidRequest { message } => message.clone(),
        other => other.to_string(),
    }
}

/// The error the call ends with when the server answers `response`.
async fn failure(wire: &Wire, response: String, options: RequestOptions) -> ProviderError {
    let (url, _served) = serve_once(response);
    let provider = (wire.build)(url, options);
    match provider.stream(request(), CancellationToken::new()).await {
        Err(err) => err,
        Ok(stream) => match stream.collect::<Vec<_>>().await.into_iter().last() {
            Some(Err(err)) => err,
            other => panic!("{}: the call fails, got {other:?}", wire.name),
        },
    }
}

fn http_response(status: u16, body: &str) -> String {
    format!(
        "HTTP/1.1 {status} Bad Request\r\ncontent-type: application/json\r\n\
         content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn stream_response(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\
         connection: close\r\n\r\n{body}",
        body.len()
    )
}

/// The secrets of one kind, and the options that make the adapter scrub it.
fn cases() -> Vec<(&'static str, &'static str, RequestOptions)> {
    let mut salted = RequestOptions::default();
    salted.cache_salt = CacheSalt::new(SECRET);
    vec![
        (
            "a header value",
            SECRET,
            RequestOptions::default()
                .header("x-gateway-token", SECRET)
                .unwrap(),
        ),
        ("the cache salt", SECRET, salted),
        ("the provider key", KEY, RequestOptions::default()),
    ]
}

/// Every filler length that puts `secret` across the cut at character
/// 500, and a few either side.
const FILLERS: std::ops::RangeInclusive<usize> = 440..=500;

async fn assert_no_part_of_a_secret(path: &str, answer: impl Fn(&Wire, &str) -> String) {
    for wire in wires() {
        for (what, secret, options) in cases() {
            let mut leaked = Vec::new();
            for filler in FILLERS {
                let echoed = format!("{}{secret}", "e".repeat(filler));
                let err = failure(&wire, answer(&wire, &echoed), options.clone()).await;
                let shown = message(&err);
                assert!(
                    shown.chars().count() <= MAX_ERROR_CHARS,
                    "{} {path}: {} characters",
                    wire.name,
                    shown.chars().count()
                );
                if shown.contains(&secret[..8]) {
                    leaked.push(filler);
                }
            }
            assert!(
                leaked.is_empty(),
                "{} {path}: {what} left in the error text at filler lengths {leaked:?}",
                wire.name
            );
        }
    }
}

#[tokio::test]
async fn a_secret_that_straddles_the_cut_of_a_stream_error_is_removed() {
    assert_no_part_of_a_secret("stream error", |wire, echoed| {
        stream_response(&(wire.stream)(echoed))
    })
    .await;
}

#[tokio::test]
async fn a_secret_that_straddles_the_cut_of_an_http_error_is_removed() {
    assert_no_part_of_a_secret("http error", |wire, echoed| {
        let (status, body) = (wire.http)(echoed);
        http_response(status, &body)
    })
    .await;
}

#[tokio::test]
async fn error_text_is_cut_to_the_limit_when_nothing_is_set_to_scrub() {
    for wire in wires() {
        let long = "e".repeat(8 * 1024);
        let err = failure(
            &wire,
            stream_response(&(wire.stream)(&long)),
            RequestOptions::default(),
        )
        .await;
        assert_eq!(
            message(&err).chars().count(),
            MAX_ERROR_CHARS,
            "{}",
            wire.name
        );
        let (status, body) = (wire.http)(&long);
        let err = failure(
            &wire,
            http_response(status, &body),
            RequestOptions::default(),
        )
        .await;
        assert_eq!(
            message(&err).chars().count(),
            MAX_ERROR_CHARS,
            "{}",
            wire.name
        );
    }
}
