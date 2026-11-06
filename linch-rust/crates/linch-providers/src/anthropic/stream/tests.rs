use std::convert::Infallible;

use futures::{StreamExt, stream};
use linch_core::{
    ContentBlock, ProviderError, ProviderEvent, RedactedThinkingBlock, StopReason, ThinkingBlock,
    Usage, assemble,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::{Translator, events};
use crate::ApiKey;

const TEXT: &str = include_str!("../../../tests/fixtures/anthropic_text_thinking.sse");
const TOOLS: &str = include_str!("../../../tests/fixtures/anthropic_parallel_tools.sse");
const EDGE: &str = include_str!("../../../tests/fixtures/anthropic_edge.sse");

const START: &str = r#"{"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":5,"output_tokens":1}}}"#;
const STOP: &str = r#"{"type":"message_stop"}"#;

fn key() -> ApiKey {
    ApiKey::new("sk-ant-test").unwrap()
}

fn translator() -> Translator {
    Translator::new("claude-requested".into(), key())
}

/// Run `sse` through the stream driver, split into `chunk`-byte pieces.
async fn run(sse: &str, chunk: usize) -> Vec<Result<ProviderEvent, ProviderError>> {
    let pieces: Vec<Result<Vec<u8>, Infallible>> = sse
        .as_bytes()
        .chunks(chunk)
        .map(|piece| Ok(piece.to_vec()))
        .collect();
    events(
        stream::iter(pieces),
        translator(),
        CancellationToken::new(),
        key(),
    )
    .collect()
    .await
}

async fn run_ok(sse: &str) -> Vec<ProviderEvent> {
    run(sse, 4096)
        .await
        .into_iter()
        .collect::<Result<_, _>>()
        .expect("fixture streams cleanly")
}

/// Data-only SSE frames, one per payload.
fn frames(payloads: &[&str]) -> String {
    payloads
        .iter()
        .map(|data| format!("data: {data}\n\n"))
        .collect()
}

fn last_error(mut results: Vec<Result<ProviderEvent, ProviderError>>) -> ProviderError {
    let last = results.pop().expect("at least one item");
    assert!(
        results.iter().all(Result::is_ok),
        "only the last item fails"
    );
    last.expect_err("stream ends with an error")
}

#[tokio::test]
async fn recorded_text_turn_keeps_a_signature_only_thinking_block() {
    let turn = assemble(run_ok(TEXT).await).unwrap();
    assert_eq!(turn.model, "claude-sonnet-4-6");
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert_eq!(
        turn.message.content[0],
        ContentBlock::Thinking(ThinkingBlock {
            thinking: String::new(),
            signature: Some("6aff04b0-17d2-44d2-9f3e-6201c0179cbb".into()),
        })
    );
    assert_eq!(turn.message.text(), "pong");
    assert_eq!(turn.message.content.len(), 2);
    assert_eq!(
        turn.usage,
        Usage {
            input_tokens: 43,
            output_tokens: 3,
            ..Usage::default()
        }
    );
}

#[tokio::test]
async fn recorded_parallel_tool_calls_end_with_tool_use() {
    let turn = assemble(run_ok(TOOLS).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let ContentBlock::Thinking(thinking) = &turn.message.content[0] else {
        panic!("thinking first: {:?}", turn.message.content);
    };
    assert!(thinking.thinking.starts_with("The user wants"));
    assert!(thinking.signature.is_some());
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].id, "call_00_cyykLObIwPinH20EybNw8457");
    assert_eq!(calls[0].input["city"], "Paris");
    assert_eq!(calls[1].input["city"], "Tokyo");
    assert_eq!(turn.usage.input_tokens, 306);
    assert_eq!(turn.usage.output_tokens, 92);
}

#[tokio::test]
async fn split_bytes_give_the_same_events() {
    for fixture in [TEXT, TOOLS, EDGE] {
        let whole = run_ok(fixture).await;
        let split: Vec<_> = run(fixture, 7)
            .await
            .into_iter()
            .map(Result::unwrap)
            .collect();
        assert_eq!(split, whole);
    }
}

#[tokio::test]
async fn edge_fixture_covers_redacted_prefilled_and_skipped_blocks() {
    let turn = assemble(run_ok(EDGE).await).unwrap();
    let content = &turn.message.content;
    assert_eq!(
        content[0],
        ContentBlock::RedactedThinking(RedactedThinkingBlock {
            data: "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIw".into(),
        })
    );
    // The server tool block and its delta, the unknown event and the
    // citations delta leave no trace.
    assert_eq!(content[1], ContentBlock::text("Done now."));
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls[0].id, "toolu_prefilled");
    assert_eq!(calls[0].input, *json!({"path": "."}).as_object().unwrap());
    assert!(calls[1].input.is_empty());
    assert_eq!(content.len(), 4);
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    assert_eq!(
        turn.usage,
        Usage {
            input_tokens: 12,
            output_tokens: 57,
            cache_read_tokens: 0,
            cache_creation_tokens: 2048,
        }
    );
}

#[tokio::test]
async fn stop_reasons_map_and_unknown_ones_fail() {
    for (wire, stop) in [
        ("end_turn", StopReason::EndTurn),
        ("max_tokens", StopReason::MaxTokens),
        ("model_context_window_exceeded", StopReason::MaxTokens),
        ("stop_sequence", StopReason::StopSequence),
        ("refusal", StopReason::Refusal),
    ] {
        let delta = format!(r#"{{"type":"message_delta","delta":{{"stop_reason":"{wire}"}}}}"#);
        let turn = assemble(run_ok(&frames(&[START, &delta, STOP])).await).unwrap();
        assert_eq!(turn.stop_reason, stop, "{wire}");
        assert_eq!(turn.model, "claude-x");
    }
    for wire in ["pause_turn", "paused"] {
        let delta = format!(r#"{{"type":"message_delta","delta":{{"stop_reason":"{wire}"}}}}"#);
        let err = last_error(run(&frames(&[START, &delta, STOP]), 4096).await);
        assert!(matches!(err, ProviderError::Transport { .. }), "{err}");
    }
}

#[tokio::test]
async fn error_event_mid_stream_is_a_status_error_and_ends_the_stream() {
    let sse = format!(
        "event: message_start\ndata: {START}\n\nevent: error\ndata: {}\n\ndata: {STOP}\n\n",
        r#"{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}"#
    );
    let err = last_error(run(&sse, 4096).await);
    assert!(
        matches!(&err, ProviderError::Status { status: 529, message, .. }
            if message == "overloaded_error: Overloaded"),
        "{err:?}"
    );
    assert!(err.retryable());
}

#[tokio::test]
async fn bare_error_frame_before_generation_is_a_status_error() {
    let bare = r#"{"error":{"message":"Insufficient Balance","type":"invalid_request_error"}}"#;
    let err = last_error(run(&frames(&[bare]), 4096).await);
    assert!(
        matches!(err, ProviderError::Status { status: 400, .. }),
        "{err:?}"
    );
}

#[tokio::test]
async fn broken_streams_are_transport_errors() {
    let text_start =
        r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#;
    let tool_delta = r#"{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{"}}"#;
    let end = r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"}}"#;
    for sse in [
        frames(&[START]),
        frames(&[START, STOP]),
        frames(&[text_start]),
        frames(&[START, START]),
        frames(&[START, tool_delta]),
        frames(&[START, text_start, tool_delta]),
        frames(&[START, text_start, text_start]),
        frames(&[START, r#"{"type":"content_block_stop","index":3}"#]),
        frames(&[START, "{not json"]),
        frames(&[START, end]),
    ] {
        let err = last_error(run(&sse, 4096).await);
        assert!(
            matches!(err, ProviderError::Transport { .. }),
            "{sse}: {err}"
        );
    }
}

#[tokio::test]
async fn blank_model_falls_back_to_the_requested_one() {
    let start = r#"{"type":"message_start","message":{"model":" "}}"#;
    let end = r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"}}"#;
    let turn = assemble(run_ok(&frames(&[start, end, STOP])).await).unwrap();
    assert_eq!(turn.model, "claude-requested");
    assert_eq!(turn.usage, Usage::default());
}

#[tokio::test]
async fn events_after_message_stop_are_not_read() {
    let end = r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"}}"#;
    let results = run(&frames(&[START, end, STOP, "{not json"]), 4096).await;
    assert!(results.iter().all(Result::is_ok));
    assert!(matches!(
        results.last(),
        Some(Ok(ProviderEvent::MessageEnd(_)))
    ));
}

#[tokio::test]
async fn cancel_mid_stream_ends_with_cancelled() {
    let first = frames(&[START]);
    let bytes = stream::iter([Ok::<_, Infallible>(first.into_bytes())]).chain(stream::pending());
    let cancel = CancellationToken::new();
    let mut events = events(bytes, translator(), cancel.clone(), key());
    assert!(matches!(
        events.next().await,
        Some(Ok(ProviderEvent::MessageStart(_)))
    ));
    cancel.cancel();
    assert!(matches!(
        events.next().await,
        Some(Err(ProviderError::Cancelled))
    ));
    assert!(events.next().await.is_none());
}

#[tokio::test]
async fn cancel_before_start_yields_only_cancelled() {
    let cancel = CancellationToken::new();
    cancel.cancel();
    let results: Vec<_> = events(
        stream::iter([Ok::<_, Infallible>(TEXT.as_bytes().to_vec())]),
        translator(),
        cancel,
        key(),
    )
    .collect()
    .await;
    assert!(matches!(
        results.as_slice(),
        [Err(ProviderError::Cancelled)]
    ));
}

#[tokio::test]
async fn transport_and_stream_errors_are_redacted() {
    let bytes = stream::iter([Err::<Vec<u8>, _>("reset; x-api-key: sk-ant-test")]);
    let err = last_error(
        events(bytes, translator(), CancellationToken::new(), key())
            .collect()
            .await,
    );
    assert!(!format!("{err} {err:?}").contains("sk-ant-test"), "{err}");
    let echoed = r#"{"type":"error","error":{"type":"api_error","message":"saw sk-ant-test"}}"#;
    let err = last_error(run(&frames(&[START, echoed]), 4096).await);
    assert!(!format!("{err} {err:?}").contains("sk-ant-test"), "{err}");
}
