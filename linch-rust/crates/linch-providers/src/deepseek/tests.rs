use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use futures::StreamExt;
use linch_core::provider::{OutputSchema, Thinking};
use linch_core::{
    ContentBlock, Message, ProviderError, ProviderRequest, ReasoningEffort, Role, ThinkingBlock,
    ToolChoice, ToolDefinition, ToolResultBlock, ToolUseBlock, assemble,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::*;

const SECRET: &str = "sk-unit-test-secret-9f8e7d";

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "deepseek-v4-flash".into(),
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

fn thinking(thinking: Option<Thinking>) -> ProviderRequest {
    ProviderRequest {
        thinking,
        ..request()
    }
}

fn weather_tool() -> ToolDefinition {
    ToolDefinition {
        name: "get_weather".into(),
        description: "Weather.".into(),
        input_schema: json!({"type": "object"}).as_object().unwrap().clone(),
    }
}

#[test]
fn only_disabled_turns_thinking_off() {
    let enabled = Thinking::Enabled {
        budget_tokens: Some(1024),
        display: None,
    };
    for (state, wire) in [
        (None, "enabled"),
        (Some(Thinking::Adaptive { display: None }), "enabled"),
        (Some(enabled), "enabled"),
        (Some(Thinking::Disabled {}), "disabled"),
    ] {
        let body = build_payload(&thinking(state)).unwrap();
        assert_eq!(body["thinking"], json!({"type": wire}));
    }
}

#[test]
fn base_body_streams_with_usage() {
    let body = build_payload(&request()).unwrap();
    assert_eq!(
        body,
        json!({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "stream": true,
            "stream_options": {"include_usage": true},
            "thinking": {"type": "enabled"},
        })
    );
}

#[test]
fn effort_maps_to_deepseek_levels_with_thinking_on() {
    for (effort, wire) in [
        (ReasoningEffort::Low, "low"),
        (ReasoningEffort::Medium, "high"),
        (ReasoningEffort::High, "high"),
        (ReasoningEffort::Xhigh, "high"),
        (ReasoningEffort::Max, "max"),
    ] {
        let body = build_payload(&ProviderRequest {
            effort: Some(effort),
            ..request()
        })
        .unwrap();
        assert_eq!(body["reasoning_effort"], json!(wire));
    }
}

#[test]
fn effort_is_omitted_when_thinking_is_off_or_unset() {
    let off = ProviderRequest {
        effort: Some(ReasoningEffort::Max),
        ..thinking(Some(Thinking::Disabled {}))
    };
    assert!(
        build_payload(&off)
            .unwrap()
            .get("reasoning_effort")
            .is_none()
    );
    assert!(
        build_payload(&request())
            .unwrap()
            .get("reasoning_effort")
            .is_none()
    );
}

#[test]
fn forced_tool_choice_with_thinking_on_fails_locally() {
    let forced = [
        ToolChoice::Required {},
        ToolChoice::Tool {
            name: "get_weather".into(),
        },
    ];
    for choice in forced.clone() {
        for state in [None, Some(Thinking::Adaptive { display: None })] {
            let err = build_payload(&ProviderRequest {
                tool_choice: Some(choice.clone()),
                tools: vec![weather_tool()],
                ..thinking(state)
            })
            .unwrap_err();
            assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
        }
        let off = build_payload(&ProviderRequest {
            tool_choice: Some(choice),
            ..thinking(Some(Thinking::Disabled {}))
        });
        assert!(off.is_ok());
    }
    for choice in [ToolChoice::Auto {}, ToolChoice::Disabled {}] {
        let body = build_payload(&ProviderRequest {
            tool_choice: Some(choice),
            ..request()
        })
        .unwrap();
        assert!(body["tool_choice"].is_string());
    }
}

#[test]
fn tool_loop_body_carries_tools_calls_reasoning_and_results() {
    let call = ToolUseBlock {
        id: "c1".into(),
        name: "get_weather".into(),
        input: json!({"city": "Paris"}).as_object().unwrap().clone(),
    };
    let body = build_payload(&ProviderRequest {
        tools: vec![weather_tool()],
        messages: vec![
            Message::user_text("weather?"),
            Message::new(
                Role::Assistant,
                vec![
                    ContentBlock::Thinking(ThinkingBlock {
                        thinking: "call it".into(),
                        signature: None,
                    }),
                    ContentBlock::ToolUse(call),
                ],
            ),
            Message::new(
                Role::User,
                vec![ContentBlock::ToolResult(ToolResultBlock::text(
                    "c1", "18C", false,
                ))],
            ),
        ],
        ..request()
    })
    .unwrap();
    assert_eq!(body["tools"][0]["function"]["name"], "get_weather");
    assert_eq!(body["messages"][1]["reasoning_content"], "call it");
    assert_eq!(body["messages"][1]["tool_calls"][0]["id"], "c1");
    assert_eq!(
        body["messages"][2],
        json!({"role": "tool", "tool_call_id": "c1", "content": "18C"})
    );
}

#[test]
fn sampling_controls_are_forwarded() {
    let body = build_payload(&ProviderRequest {
        max_output_tokens: Some(256),
        temperature: Some(0.5),
        stop_sequences: vec!["END".into()],
        ..request()
    })
    .unwrap();
    assert_eq!(body["max_tokens"], 256);
    assert_eq!(body["temperature"], 0.5);
    assert_eq!(body["stop"], json!(["END"]));
}

#[test]
fn output_schema_is_rejected_until_supported() {
    let err = build_payload(&ProviderRequest {
        output_schema: Some(OutputSchema {
            name: "s".into(),
            schema: serde_json::Map::new(),
            strict: true,
            description: None,
        }),
        ..request()
    })
    .unwrap_err();
    assert!(matches!(err, ProviderError::InvalidRequest { .. }));
}

#[test]
fn debug_output_never_shows_the_key() {
    let options = DeepSeekOptions::new(ApiKey::new(SECRET).unwrap());
    let provider = DeepSeek::new(options.clone()).unwrap();
    for text in [format!("{options:?}"), format!("{provider:?}")] {
        assert!(!text.contains(SECRET), "{text}");
    }
}

/// Serve one canned HTTP response on a local port; the handle returns the
/// raw request it read.
fn serve_once(response: String) -> (String, JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        let (socket, _) = listener.accept().unwrap();
        let mut reader = BufReader::new(socket);
        let mut head = String::new();
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse().unwrap();
            }
            head.push_str(&line);
            if line == "\r\n" {
                break;
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        reader.get_mut().write_all(response.as_bytes()).unwrap();
        head + &String::from_utf8(body).unwrap()
    });
    (url, handle)
}

fn provider(base_url: String) -> DeepSeek {
    DeepSeek::new(DeepSeekOptions {
        api_key: ApiKey::new(SECRET).unwrap(),
        base_url,
    })
    .unwrap()
}

#[tokio::test]
async fn http_success_streams_events_and_sends_bearer_auth() {
    let sse = include_str!("../../tests/fixtures/deepseek_text_thinking.sse");
    let (url, server) = serve_once(format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n{sse}"
    ));
    let stream = provider(url + "/")
        .stream(request(), CancellationToken::new())
        .await
        .unwrap();
    let events: Vec<_> = stream.map(Result::unwrap).collect().await;
    assert_eq!(assemble(events).unwrap().model, "deepseek-flash");
    let raw = server.join().unwrap();
    assert!(raw.starts_with("POST /chat/completions HTTP/1.1"), "{raw}");
    assert!(
        raw.contains(&format!("authorization: Bearer {SECRET}")),
        "{raw}"
    );
    assert!(raw.contains("\"include_usage\":true"), "{raw}");
}

#[tokio::test]
async fn http_429_maps_status_and_retry_after_without_the_key() {
    let body = format!(
        r#"{{"error":{{"message":"Rate limit reached for Authorization: Bearer {SECRET}","type":"rate_limit_error"}}}}"#
    );
    let (url, server) = serve_once(format!(
        "HTTP/1.1 429 Too Many Requests\r\nretry-after: 3\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
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
            if *delay == Duration::from_secs(3)),
        "{err:?}"
    );
    assert!(!format!("{err} {err:?}").contains(SECRET), "{err:?}");
}

#[tokio::test]
async fn invalid_request_fails_before_any_http() {
    // No server listens here; a local check must fail first.
    let err = provider("http://127.0.0.1:9".into())
        .stream(
            ProviderRequest {
                tool_choice: Some(ToolChoice::Required {}),
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
