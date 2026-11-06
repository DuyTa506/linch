use std::time::Duration;

use futures::StreamExt;
use linch_core::{Message, OutputSchema, ProviderError, ProviderRequest, assemble};
use tokio_util::sync::CancellationToken;

use super::*;
use crate::test_server::{assert_redirect_refused, redirect_response, serve_once};

mod payload;

const SECRET: &str = "sk-ant-unit-test-secret-4b5c";

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "claude-sonnet-4-6".into(),
        system: vec![],
        tools: vec![],
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

#[test]
fn debug_output_never_shows_the_key() {
    let options = AnthropicOptions::new(ApiKey::new(SECRET).unwrap());
    let provider = Anthropic::new(options.clone()).unwrap();
    for text in [format!("{options:?}"), format!("{provider:?}")] {
        assert!(!text.contains(SECRET), "{text}");
    }
}

fn provider(base_url: String) -> Anthropic {
    Anthropic::new(AnthropicOptions {
        api_key: ApiKey::new(SECRET).unwrap(),
        base_url,
    })
    .unwrap()
}

#[tokio::test]
async fn http_success_streams_events_with_native_headers() {
    let sse = include_str!("../../tests/fixtures/anthropic_text_thinking.sse");
    let (url, server) = serve_once(format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}"
    ));
    let stream = provider(url + "/anthropic/")
        .stream(request(), CancellationToken::new())
        .await
        .unwrap();
    let events: Vec<_> = stream.map(Result::unwrap).collect().await;
    assert_eq!(assemble(events).unwrap().message.text(), "pong");
    let raw = server.join().unwrap();
    assert!(
        raw.starts_with("POST /anthropic/v1/messages HTTP/1.1"),
        "{raw}"
    );
    assert!(raw.contains(&format!("x-api-key: {SECRET}")), "{raw}");
    assert!(raw.contains("anthropic-version: 2023-06-01"), "{raw}");
    assert!(!raw.to_ascii_lowercase().contains("authorization"), "{raw}");
    assert!(raw.contains("\"stream\":true"), "{raw}");
}

#[tokio::test]
async fn http_429_maps_status_and_retry_after_without_the_key() {
    let body = format!(
        r#"{{"type":"error","error":{{"type":"rate_limit_error","message":"slow down, x-api-key: {SECRET}"}}}}"#
    );
    let (url, server) = serve_once(format!(
        "HTTP/1.1 429 Too Many Requests\r\nretry-after: 4\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    ));
    let err = provider(url)
        .stream(request(), CancellationToken::new())
        .await
        .err()
        .expect("429 fails the call");
    server.join().unwrap();
    assert!(
        matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), .. }
            if *delay == Duration::from_secs(4)),
        "{err:?}"
    );
    assert!(!format!("{err} {err:?}").contains(SECRET), "{err:?}");
}

#[tokio::test]
async fn invalid_request_fails_before_any_http() {
    let err = provider("http://127.0.0.1:9".into())
        .stream(
            ProviderRequest {
                output_schema: Some(OutputSchema {
                    name: "s".into(),
                    schema: serde_json::Map::new(),
                    strict: true,
                    description: None,
                }),
                ..request()
            },
            CancellationToken::new(),
        )
        .await
        .err()
        .unwrap();
    assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
}

#[tokio::test]
async fn cancelled_before_send_is_cancelled() {
    let cancel = CancellationToken::new();
    cancel.cancel();
    let err = provider("http://127.0.0.1:9".into())
        .stream(request(), cancel)
        .await
        .err()
        .unwrap();
    assert!(matches!(err, ProviderError::Cancelled), "{err}");
}

#[tokio::test]
async fn a_redirect_is_refused_and_never_followed() {
    let (url, server) = serve_once(redirect_response());
    let err = provider(url)
        .stream(request(), CancellationToken::new())
        .await
        .err()
        .expect("a redirect fails the call");
    server.join().unwrap();
    assert_redirect_refused(&err);
}
