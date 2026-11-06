use std::time::Duration;

use futures::StreamExt;
use linch_core::provider::{OutputSchema, Thinking};
use linch_core::{
    ContentBlock, Message, ProviderError, ProviderRequest, ReasoningEffort, Role, StopReason,
    ThinkingBlock, ToolChoice, Usage,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::test_support::{
    assemble_ok, bytes, error_response, request, serve_once, sse_response, weather_tool,
};
use super::*;

const SECRET: &str = "sk-unit-test-openai-4c3b2a";
const MODEL: &str = "gpt-5.5";
const TOOLS: &str = include_str!("../../tests/fixtures/openai_chat_tools.sse");

fn body(request: &ProviderRequest) -> serde_json::Value {
    profile::build_payload(&profile::OPENAI, &JsonObject::new(), request).unwrap()
}

#[test]
fn base_body_streams_with_usage_and_no_thinking_switch() {
    let disabled = ProviderRequest {
        thinking: Some(Thinking::Disabled {}),
        ..request(MODEL)
    };
    assert_eq!(
        body(&disabled),
        json!({
            "model": MODEL,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "stream": true,
            "stream_options": {"include_usage": true},
        })
    );
}

#[test]
fn output_limit_is_max_completion_tokens() {
    let limited = ProviderRequest {
        max_output_tokens: Some(512),
        ..request(MODEL)
    };
    let body = body(&limited);
    assert_eq!(body["max_completion_tokens"], 512);
    assert!(body.get("max_tokens").is_none());
}

#[test]
fn effort_uses_openai_levels_whatever_the_thinking_setting() {
    for (effort, wire) in [
        (ReasoningEffort::Low, "low"),
        (ReasoningEffort::Medium, "medium"),
        (ReasoningEffort::High, "high"),
        (ReasoningEffort::Xhigh, "xhigh"),
        (ReasoningEffort::Max, "max"),
    ] {
        for thinking in [None, Some(Thinking::Disabled {})] {
            let body = body(&ProviderRequest {
                effort: Some(effort),
                thinking,
                ..request(MODEL)
            });
            assert_eq!(body["reasoning_effort"], wire);
        }
    }
}

#[test]
fn earlier_reasoning_is_not_sent_back() {
    let turn = Message::new(
        Role::Assistant,
        vec![
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "from another vendor".into(),
                signature: None,
            }),
            ContentBlock::text("done"),
        ],
    );
    let body = body(&ProviderRequest {
        messages: vec![Message::user_text("hi"), turn],
        ..request(MODEL)
    });
    assert_eq!(
        body["messages"][1],
        json!({"role": "assistant", "content": "done"})
    );
}

#[test]
fn every_tool_choice_is_accepted() {
    for (choice, wire) in [
        (ToolChoice::Auto {}, json!("auto")),
        (ToolChoice::Disabled {}, json!("none")),
        (ToolChoice::Required {}, json!("required")),
        (
            ToolChoice::Tool {
                name: "get_weather".into(),
            },
            json!({"type": "function", "function": {"name": "get_weather"}}),
        ),
    ] {
        let body = body(&ProviderRequest {
            tools: vec![weather_tool()],
            tool_choice: Some(choice),
            ..request(MODEL)
        });
        assert_eq!(body["tool_choice"], wire);
        assert_eq!(body["tools"][0]["function"]["name"], "get_weather");
    }
}

#[test]
fn sampling_controls_are_forwarded() {
    let body = body(&ProviderRequest {
        temperature: Some(0.2),
        stop_sequences: vec!["END".into()],
        ..request(MODEL)
    });
    assert_eq!(body["temperature"], 0.2);
    assert_eq!(body["stop"], json!(["END"]));
}

#[test]
fn output_schema_is_rejected_until_supported() {
    let err = profile::build_payload(
        &profile::OPENAI,
        &JsonObject::new(),
        &ProviderRequest {
            output_schema: Some(OutputSchema {
                name: "s".into(),
                schema: JsonObject::new(),
                strict: true,
                description: None,
            }),
            ..request(MODEL)
        },
    )
    .unwrap_err();
    assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
}

#[tokio::test]
async fn fixture_parallel_calls_and_cache_usage_assemble() {
    for chunk in [7, 1 << 20] {
        let events = stream::events(
            bytes(TOOLS, chunk),
            Translator::new(MODEL.into(), None, profile::OPENAI.usage),
            CancellationToken::new(),
            None,
        );
        let turn = assemble_ok(events).await;
        assert_eq!(turn.model, "gpt-5.5-2026-08-01");
        assert_eq!(turn.stop_reason, StopReason::ToolUse);
        assert_eq!(turn.message.text(), "Checking both.");
        let calls: Vec<_> = turn.message.tool_uses().collect();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].id, "call_paris");
        assert_eq!(
            calls[1].input,
            *json!({"city": "Tokyo"}).as_object().unwrap()
        );
        // 1200 prompt tokens = 1024 cache reads + 100 cache writes + 76.
        assert_eq!(
            turn.usage.unwrap(),
            Usage {
                input_tokens: 76,
                output_tokens: 64,
                cache_read_tokens: 1024,
                cache_creation_tokens: 100,
            }
        );
    }
}

fn provider(base_url: String) -> OpenAiChat {
    OpenAiChat::new(OpenAiChatOptions {
        api_key: ApiKey::new(SECRET).unwrap(),
        base_url,
    })
    .unwrap()
}

#[test]
fn debug_output_never_shows_the_key() {
    let options = OpenAiChatOptions::new(ApiKey::new(SECRET).unwrap());
    assert_eq!(options.base_url, DEFAULT_BASE_URL);
    let provider = OpenAiChat::new(options.clone()).unwrap();
    assert_eq!(provider.id(), "openai-chat");
    for text in [format!("{options:?}"), format!("{provider:?}")] {
        assert!(!text.contains(SECRET), "{text}");
    }
}

#[tokio::test]
async fn http_round_trip_posts_chat_completions_with_bearer_auth() {
    let (url, server) = serve_once(sse_response(TOOLS));
    let stream = provider(url + "/v1/")
        .stream(
            ProviderRequest {
                max_output_tokens: Some(64),
                ..request(MODEL)
            },
            CancellationToken::new(),
        )
        .await
        .unwrap();
    assert_eq!(assemble_ok(stream).await.stop_reason, StopReason::ToolUse);
    let raw = server.join().unwrap();
    assert!(
        raw.starts_with("POST /v1/chat/completions HTTP/1.1"),
        "{raw}"
    );
    assert!(
        raw.contains(&format!("authorization: Bearer {SECRET}")),
        "{raw}"
    );
    assert!(raw.contains("\"max_completion_tokens\":64"), "{raw}");
}

#[tokio::test]
async fn http_429_keeps_retry_after_and_never_the_key() {
    let body = format!(
        r#"{{"error":{{"message":"Rate limit reached; saw Authorization: Bearer {SECRET}","type":"rate_limit_error","param":null,"code":"slow_down"}}}}"#
    );
    let (url, server) = serve_once(error_response(
        "429 Too Many Requests",
        "retry-after: 56\r\n",
        &body,
    ));
    let err = provider(url)
        .stream(request(MODEL), CancellationToken::new())
        .await
        .err()
        .expect("429 fails the call");
    server.join().unwrap();
    assert!(
        matches!(&err, ProviderError::Status { status: 429, retry_after: Some(delay), message }
            if *delay == Duration::from_secs(56) && message.starts_with("rate_limit_error: ")),
        "{err:?}"
    );
    assert!(err.retryable());
    assert!(!format!("{err} {err:?}").contains(SECRET), "{err:?}");
}

#[tokio::test]
async fn an_error_frame_after_200_fails_the_stream() {
    let sse = "data: {\"error\":{\"message\":\"The server had an error\",\"type\":\"server_error\",\"param\":null,\"code\":null}}\n\n";
    let (url, server) = serve_once(sse_response(sse));
    let results: Vec<_> = provider(url)
        .stream(request(MODEL), CancellationToken::new())
        .await
        .unwrap()
        .collect()
        .await;
    server.join().unwrap();
    assert!(
        matches!(results.as_slice(), [Err(ProviderError::Transport { message })]
            if message == "stream error: server_error: The server had an error"),
        "{results:?}"
    );
}

#[tokio::test]
async fn invalid_request_fails_before_any_http() {
    // No server listens here; the local check must fail first.
    let err = provider("http://127.0.0.1:9".into())
        .stream(
            ProviderRequest {
                output_schema: Some(OutputSchema {
                    name: "s".into(),
                    schema: JsonObject::new(),
                    strict: true,
                    description: None,
                }),
                ..request(MODEL)
            },
            CancellationToken::new(),
        )
        .await
        .err()
        .unwrap();
    assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
}
