use std::convert::Infallible;

use futures::{StreamExt, stream};
use linch_core::{
    ContentBlock, ProviderError, ProviderEvent, StopReason, StreamError, Usage, assemble,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::{ReasoningField, Translator, events};
use crate::ApiKey;
use crate::deepseek::usage;

mod frames;

const TEXT: &str = include_str!("../../../tests/fixtures/deepseek_text_thinking.sse");
const TOOLS: &str = include_str!("../../../tests/fixtures/deepseek_parallel_tools.sse");
const EDGE: &str = include_str!("../../../tests/fixtures/edge_stop_with_tool_calls.sse");

fn key() -> Option<ApiKey> {
    ApiKey::new("sk-test")
}

fn translator() -> Translator {
    Translator::new(
        "deepseek-v4-flash".into(),
        Some(ReasoningField::ReasoningContent),
        usage,
    )
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

async fn run_ok(sse: &str, chunk: usize) -> Vec<ProviderEvent> {
    run(sse, chunk)
        .await
        .into_iter()
        .collect::<Result<_, _>>()
        .expect("fixture streams cleanly")
}

fn last_error(results: Vec<Result<ProviderEvent, ProviderError>>) -> ProviderError {
    let mut results = results;
    let last = results.pop().expect("at least one item");
    assert!(
        results.iter().all(Result::is_ok),
        "only the last item fails"
    );
    last.expect_err("stream ends with an error")
}

#[tokio::test]
async fn recorded_text_turn_assembles_thinking_then_text() {
    let turn = assemble(run_ok(TEXT, 4096).await).unwrap();
    assert_eq!(turn.model, "deepseek-flash");
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert!(
        matches!(&turn.message.content[0], ContentBlock::Thinking(block) if !block.thinking.is_empty())
    );
    assert!(matches!(&turn.message.content[1], ContentBlock::Text(_)));
    assert_eq!(turn.message.content.len(), 2);
    assert_eq!(
        turn.usage,
        Some(Usage {
            input_tokens: 39,
            output_tokens: 42,
            ..Usage::default()
        })
    );
}

#[tokio::test]
async fn recorded_parallel_tool_calls_map_index_to_id() {
    let turn = assemble(run_ok(TOOLS, 4096).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].id, "call_00_JiQq3wRVYVeWXsbzjRrC4678");
    assert_eq!(
        calls[0].input,
        *json!({"city": "Paris"}).as_object().unwrap()
    );
    assert_eq!(calls[1].id, "call_01_L3jCp7ziopLHzGJFldP97218");
    assert_eq!(
        calls[1].input,
        *json!({"city": "Tokyo"}).as_object().unwrap()
    );
    let usage = turn.usage.expect("the recording reports usage");
    assert_eq!(usage.input_tokens, 308);
    assert_eq!(usage.output_tokens, 96);
}

#[tokio::test]
async fn split_bytes_give_the_same_events() {
    for fixture in [TEXT, TOOLS, EDGE] {
        assert_eq!(run_ok(fixture, 7).await, run_ok(fixture, 1 << 20).await);
    }
}

#[tokio::test]
async fn stop_with_tool_calls_and_usage_only_chunk() {
    let events = run_ok(EDGE, 64).await;
    // The argument-less call gets `{}` before its end.
    assert!(events.contains(&ProviderEvent::ToolUseInputDelta(
        linch_core::provider::ToolUseInputDeltaEvent {
            id: "call_a".into(),
            json_delta: "{}".into(),
        }
    )));
    let turn = assemble(events).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls[0].name, "list_files");
    assert!(calls[0].input.is_empty());
    assert_eq!(
        calls[1].input,
        *json!({"path": "a.txt"}).as_object().unwrap()
    );
    assert_eq!(
        turn.usage,
        Some(Usage {
            input_tokens: 56,
            output_tokens: 30,
            cache_read_tokens: 64,
            cache_creation_tokens: 0,
        })
    );
}

fn chunk(choice: serde_json::Value) -> String {
    format!(
        "data: {}\n\n",
        json!({"model": "deepseek-flash", "choices": [choice]})
    )
}

#[tokio::test]
async fn eof_before_done_is_a_transport_error() {
    let sse = chunk(json!({"delta": {"content": "hi"}}));
    let err = last_error(run(&sse, 4096).await);
    assert!(matches!(err, ProviderError::Transport { .. }), "{err}");
    assert!(err.retryable());
}

#[tokio::test]
async fn a_last_done_with_no_blank_line_after_it_is_read() {
    let sse = chunk(json!({"delta": {"content": "hi"}, "finish_reason": "stop"}));
    for done in ["data: [DONE]\n", "data: [DONE]"] {
        let turn = assemble(run_ok(&(sse.clone() + done), 4096).await).unwrap();
        assert_eq!(turn.stop_reason, StopReason::EndTurn, "{done:?}");
    }
}

#[tokio::test]
async fn a_finished_response_is_drained_before_a_cancel() {
    // The tool call and the end are both queued at [DONE]; a cancel that
    // comes while they wait must not lose the end and its usage.
    let tool = json!({"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}});
    let sse = chunk(json!({"delta": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}))
        + &format!(
            "data: {}\n\n",
            json!({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        )
        + "data: [DONE]\n\n";
    let bytes = stream::iter([Ok::<_, Infallible>(sse.into_bytes())]).chain(stream::pending());
    let cancel = CancellationToken::new();
    let mut events = events(bytes, translator(), cancel.clone(), key());
    while !matches!(events.next().await, Some(Ok(ProviderEvent::ToolUseEnd(_)))) {}
    cancel.cancel();
    let rest: Vec<_> = events.collect().await;
    assert!(
        matches!(rest.as_slice(), [Ok(ProviderEvent::MessageEnd(end))]
            if end.usage.is_some_and(|usage| usage.output_tokens == 2)),
        "{rest:?}"
    );
}

#[tokio::test]
async fn done_without_finish_reason_is_a_transport_error() {
    let sse = chunk(json!({"delta": {"content": "hi"}})) + "data: [DONE]\n\n";
    let err = last_error(run(&sse, 4096).await);
    assert!(err.to_string().contains("finish_reason"), "{err}");
}

#[tokio::test]
async fn done_before_any_chunk_is_a_transport_error() {
    let err = last_error(run("data: [DONE]\n\n", 4096).await);
    assert!(matches!(err, ProviderError::Transport { .. }), "{err}");
}

#[tokio::test]
async fn malformed_chunk_is_a_transport_error() {
    let err = last_error(run("data: {not json\n\n", 4096).await);
    assert!(err.to_string().contains("malformed"), "{err}");
}

#[tokio::test]
async fn abnormal_finish_reasons_fail_the_call() {
    for reason in ["insufficient_system_resource", "aborted", "something_new"] {
        let sse = chunk(json!({"delta": {"content": "par"}, "finish_reason": reason}));
        let err = last_error(run(&sse, 4096).await);
        assert!(err.to_string().contains(reason), "{err}");
        assert!(err.retryable());
    }
}

#[tokio::test]
async fn finish_reasons_map_to_stop_reasons() {
    for (reason, stop) in [
        ("stop", StopReason::EndTurn),
        ("length", StopReason::MaxTokens),
        ("content_filter", StopReason::Refusal),
    ] {
        let sse =
            chunk(json!({"delta": {"content": "x"}, "finish_reason": reason})) + "data: [DONE]\n\n";
        assert_eq!(
            assemble(run_ok(&sse, 4096).await).unwrap().stop_reason,
            stop
        );
    }
}

/// One tool-call piece for call `index`.
fn call_piece(index: u32, id: &str, arguments: &str) -> serde_json::Value {
    json!({"index": index, "id": id, "type": "function",
        "function": {"name": "get_weather", "arguments": arguments}})
}

/// R1-prov 5: a call cut off by `length` is dropped and the turn reports
/// MaxTokens; arguments still stream as they arrive.
#[tokio::test]
async fn a_length_stop_drops_a_cut_off_call_and_keeps_max_tokens() {
    for arguments in ["{\"city\": \"Par", ""] {
        let sse = chunk(json!({"delta": {"content": "Checking."}}))
            + &chunk(json!({"delta": {"tool_calls": [call_piece(0, "c1", arguments)]}}))
            + &chunk(json!({"delta": {}, "finish_reason": "length"}))
            + "data: [DONE]\n\n";
        let turn = assemble(run_ok(&sse, 4096).await).unwrap();
        assert_eq!(turn.stop_reason, StopReason::MaxTokens, "{arguments:?}");
        assert_eq!(turn.message.tool_uses().count(), 0);
        assert_eq!(turn.message.text(), "Checking.");
    }
}

#[tokio::test]
async fn a_length_stop_keeps_the_calls_that_finished() {
    let sse =
        chunk(json!({"delta": {"tool_calls": [call_piece(0, "c1", "{\"city\": \"Paris\"}")]}}))
            + &chunk(json!({"delta": {"tool_calls": [call_piece(1, "c2", "{\"city\": \"Tok")]}}))
            + &chunk(json!({"delta": {}, "finish_reason": "length"}))
            + "data: [DONE]\n\n";
    let turn = assemble(run_ok(&sse, 4096).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let ids: Vec<_> = (turn.message.tool_uses())
        .map(|call| call.id.as_str())
        .collect();
    assert_eq!(ids, ["c1"]);
}

#[tokio::test]
async fn a_cut_off_call_without_a_length_stop_is_still_invalid() {
    let sse = chunk(json!({"delta": {"tool_calls": [call_piece(0, "c1", "{\"city\": \"Par")]}}))
        + &chunk(json!({"delta": {}, "finish_reason": "tool_calls"}))
        + "data: [DONE]\n\n";
    let err = assemble(run_ok(&sse, 4096).await).unwrap_err();
    assert!(
        matches!(&err, StreamError::InvalidToolInput { id, .. } if id == "c1"),
        "{err:?}"
    );
}

#[tokio::test]
async fn a_stream_without_usage_reports_unknown_usage() {
    let sse =
        chunk(json!({"delta": {"content": "x"}, "finish_reason": "stop"})) + "data: [DONE]\n\n";
    assert_eq!(assemble(run_ok(&sse, 4096).await).unwrap().usage, None);
}

#[tokio::test]
async fn missing_model_falls_back_to_the_requested_one() {
    let sse = format!(
        "data: {}\n\ndata: [DONE]\n\n",
        json!({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
    );
    assert_eq!(
        assemble(run_ok(&sse, 4096).await).unwrap().model,
        "deepseek-v4-flash"
    );
}

#[tokio::test]
async fn cancel_mid_stream_ends_with_cancelled() {
    let first = chunk(json!({"delta": {"content": "hi"}}));
    let bytes = stream::iter([Ok::<_, Infallible>(first.into_bytes())]).chain(stream::pending());
    let cancel = CancellationToken::new();
    let mut events = events(bytes, translator(), cancel.clone(), key());
    assert!(matches!(
        events.next().await,
        Some(Ok(ProviderEvent::MessageStart(_)))
    ));
    assert!(matches!(
        events.next().await,
        Some(Ok(ProviderEvent::TextDelta(_)))
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
async fn transport_errors_are_redacted() {
    let bytes = stream::iter([Err::<Vec<u8>, _>("reset; Authorization: Bearer sk-test")]);
    let err = last_error(
        events(bytes, translator(), CancellationToken::new(), key())
            .collect()
            .await,
    );
    assert!(!format!("{err} {err:?}").contains("sk-test"), "{err}");
}
