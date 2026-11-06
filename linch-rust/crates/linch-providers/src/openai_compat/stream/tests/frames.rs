//! Error frames, the reasoning field choice and late tool names.

use std::convert::Infallible;

use futures::{StreamExt, stream};
use linch_core::{ContentBlock, ProviderError, ProviderEvent, StopReason, assemble};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::super::{ReasoningField, Translator, events};
use super::{chunk, key, last_error};
use crate::deepseek::usage;

async fn run_with(
    field: Option<ReasoningField>,
    sse: &str,
) -> Vec<Result<ProviderEvent, ProviderError>> {
    let bytes = stream::iter([Ok::<_, Infallible>(sse.as_bytes().to_vec())]);
    let translator = Translator::new("m".into(), field, usage);
    events(bytes, translator, CancellationToken::new(), key())
        .collect()
        .await
}

fn error_frame(error: serde_json::Value) -> String {
    format!("data: {}\n\n", json!({ "error": error }))
}

#[tokio::test]
async fn an_error_frame_with_an_http_code_keeps_that_status() {
    // llama.cpp and SGLang shape; the message echoes the bearer token.
    let sse = chunk(json!({"delta": {"content": "par"}}))
        + &error_frame(json!({
            "code": 503, "type": "unavailable_error",
            "message": "overloaded; Authorization: Bearer sk-test"
        }))
        + "data: [DONE]\n\n";
    let err = last_error(run_with(None, &sse).await);
    assert!(
        matches!(&err, ProviderError::Status { status: 503, retry_after: None, message }
            if message == "stream error: unavailable_error: overloaded; Authorization: Bearer [redacted]"),
        "{err:?}"
    );
    assert!(err.retryable());
    let bad = error_frame(
        json!({"object": "error", "code": 400, "type": "BadRequestError", "message": "too long"}),
    );
    let err = last_error(run_with(None, &bad).await);
    assert!(
        matches!(err, ProviderError::Status { status: 400, .. }),
        "{err:?}"
    );
    assert!(!err.retryable());
}

#[tokio::test]
async fn a_string_code_or_type_maps_to_the_status_openai_sends_for_it() {
    for (error, status, retryable) in [
        (
            json!({"message": "m", "type": "invalid_request_error", "code": "context_length_exceeded"}),
            400,
            false,
        ),
        (
            json!({"message": "m", "type": "invalid_request_error", "code": "invalid_api_key"}),
            401,
            false,
        ),
        (
            json!({"message": "m", "type": "invalid_request_error", "code": null}),
            400,
            false,
        ),
        (
            json!({"message": "m", "type": "tokens", "code": "rate_limit_exceeded"}),
            429,
            true,
        ),
        (
            json!({"message": "m", "type": "server_error", "code": null}),
            500,
            true,
        ),
    ] {
        let err = last_error(run_with(None, &error_frame(error.clone())).await);
        assert!(
            matches!(&err, ProviderError::Status { status: s, retry_after: None, .. } if *s == status),
            "{error}: {err:?}"
        );
        assert_eq!(err.retryable(), retryable, "{error}");
    }
}

#[tokio::test]
async fn an_error_frame_without_an_http_code_is_a_transport_error() {
    // OpenAI's shape, with a code and a type it does not document.
    let sse = error_frame(json!({"message": "boom", "type": "new_error", "code": "x"}));
    let err = last_error(run_with(None, &sse).await);
    assert!(
        matches!(&err, ProviderError::Transport { message } if message == "stream error: new_error: boom"),
        "{err:?}"
    );
    let bare = format!("data: {}\n\n", json!({"error": "Unauthorized"}));
    let err = last_error(run_with(None, &bare).await);
    assert!(err.to_string().contains("Unauthorized"), "{err}");
}

#[tokio::test]
async fn only_the_declared_reasoning_field_is_read() {
    let sse = chunk(json!({"delta": {"reasoning": "r", "reasoning_content": "rc"}}))
        + &chunk(json!({"delta": {"content": "x"}, "finish_reason": "stop"}))
        + "data: [DONE]\n\n";
    for (field, expected) in [
        (Some(ReasoningField::Reasoning), Some("r")),
        (Some(ReasoningField::ReasoningContent), Some("rc")),
        (None, None),
    ] {
        let events: Vec<_> = run_with(field, &sse)
            .await
            .into_iter()
            .map(Result::unwrap)
            .collect();
        let turn = assemble(events).unwrap();
        let thinking = turn.message.content.iter().find_map(|block| match block {
            ContentBlock::Thinking(block) => Some(block.thinking.as_str()),
            _ => None,
        });
        assert_eq!(thinking, expected, "{field:?}");
    }
}

#[tokio::test]
async fn a_name_that_grows_before_the_arguments_is_kept_whole() {
    // llama.cpp re-sends id and name while the name is still being parsed.
    let tool = |piece: serde_json::Value| chunk(json!({"delta": {"tool_calls": [piece]}}));
    let sse = tool(
        json!({"index": 0, "id": "abc", "type": "function", "function": {"name": "get_wea"}}),
    ) + &tool(
        json!({"index": 0, "id": "abc", "type": "function", "function": {"name": "get_weather"}}),
    ) + &tool(json!({"index": 0, "function": {"arguments": "{\"city\":"}}))
        + &tool(json!({"index": 0, "function": {"arguments": "\"Paris\"}"}}))
        + &chunk(json!({"delta": {}, "finish_reason": "tool_calls"}))
        + "data: [DONE]\n\n";
    let events: Vec<_> = run_with(None, &sse)
        .await
        .into_iter()
        .map(Result::unwrap)
        .collect();
    let starts: Vec<_> = events
        .iter()
        .filter(|event| matches!(event, ProviderEvent::ToolUseStart(_)))
        .collect();
    assert_eq!(starts.len(), 1);
    let turn = assemble(events).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let call = turn.message.tool_uses().next().unwrap();
    assert_eq!(
        (call.id.as_str(), call.name.as_str()),
        ("abc", "get_weather")
    );
    assert_eq!(call.input, *json!({"city": "Paris"}).as_object().unwrap());
}
