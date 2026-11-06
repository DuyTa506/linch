use std::time::Duration;

use futures::StreamExt;
use linch_core::{Message, OutputSchema, ProviderError, ProviderRequest, assemble};
use tokio_util::sync::CancellationToken;

use super::*;
use crate::test_server::{
    assert_redirect_refused, redirect_response, serve_once, serve_stalled_error,
};

mod payload;

const SECRET: &str = "AIzaSy-unit-test-secret-7d1e";

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "gemini-3.8-flash".into(),
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
    let options = GeminiOptions::new(ApiKey::new(SECRET).unwrap());
    let provider = Gemini::new(options.clone()).unwrap();
    for text in [format!("{options:?}"), format!("{provider:?}")] {
        assert!(!text.contains(SECRET), "{text}");
    }
}

fn provider(base_url: String) -> Gemini {
    Gemini::new(GeminiOptions {
        api_key: ApiKey::new(SECRET).unwrap(),
        base_url,
    })
    .unwrap()
}

#[tokio::test]
async fn http_success_streams_events_with_the_key_in_a_header() {
    let sse = include_str!("../../tests/fixtures/gemini_text_thinking.sse");
    let (url, server) = serve_once(format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}"
    ));
    let stream = provider(url + "/")
        .stream(
            ProviderRequest {
                model: "models/gemini-3.8-flash".into(),
                ..request()
            },
            CancellationToken::new(),
        )
        .await
        .unwrap();
    let events: Vec<_> = stream.map(Result::unwrap).collect().await;
    assert_eq!(assemble(events).unwrap().message.text(), "Pong");
    let raw = server.join().unwrap();
    assert!(
        raw.starts_with(
            "POST /v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse HTTP/1.1"
        ),
        "{raw}"
    );
    assert!(raw.contains(&format!("x-goog-api-key: {SECRET}")), "{raw}");
    assert_eq!(raw.matches(SECRET).count(), 1, "the key is not in the URL");
    assert!(raw.contains(r#""contents":[{"#), "{raw}");
}

#[tokio::test]
async fn http_429_maps_status_and_retry_info_without_the_key() {
    let body = format!(
        r#"{{"error":{{"code":429,"message":"Quota exceeded for key {SECRET}","status":"RESOURCE_EXHAUSTED","details":[{{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"20s"}}]}}}}"#
    );
    let (url, server) = serve_once(format!(
        "HTTP/1.1 429 Too Many Requests\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
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
            if *delay == Duration::from_secs(20)),
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

#[tokio::test]
async fn a_huge_error_body_is_read_only_in_part() {
    let url = serve_stalled_error(&format!("echo {SECRET} "));
    let provider = provider(url);
    let call = provider.stream(request(), CancellationToken::new());
    let err = tokio::time::timeout(Duration::from_secs(10), call)
        .await
        .expect("the body is not read to its end")
        .err()
        .expect("a 400 fails the call");
    assert!(
        matches!(&err, ProviderError::Status { status: 400, message, .. }
            if message.chars().count() == 500 && !message.contains(SECRET)),
        "{err:?}"
    );
}
