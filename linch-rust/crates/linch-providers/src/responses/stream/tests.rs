use std::convert::Infallible;

use futures::{StreamExt, stream};
use linch_core::{ContentBlock, ProviderError, ProviderEvent, StopReason, Usage, assemble};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use super::{METADATA_KEY, Translator, events};
use crate::ApiKey;
use crate::openai::test_support::{bytes, collect_ok};

const TOOLS: &str = include_str!("../../../tests/fixtures/responses_reasoning_tools.sse");
const TEXT: &str = include_str!("../../../tests/fixtures/responses_text.sse");

fn key() -> ApiKey {
    ApiKey::new("sk-test-responses").unwrap()
}

async fn run(sse: &str, chunk: usize) -> Vec<Result<ProviderEvent, ProviderError>> {
    events(
        bytes(sse, chunk),
        Translator::new("gpt-5.5".into()),
        CancellationToken::new(),
        key(),
    )
    .collect()
    .await
}

async fn run_ok(sse: &str) -> Vec<ProviderEvent> {
    collect_ok(events(
        bytes(sse, 11),
        Translator::new("gpt-5.5".into()),
        CancellationToken::new(),
        key(),
    ))
    .await
}

fn last_error(mut results: Vec<Result<ProviderEvent, ProviderError>>) -> ProviderError {
    let last = results.pop().expect("at least one item");
    assert!(
        results.iter().all(Result::is_ok),
        "only the last item fails"
    );
    last.expect_err("stream ends with an error")
}

/// SSE frames for `events`, each with its `event:` line.
fn frames(events: &[Value]) -> String {
    events
        .iter()
        .map(|event| {
            format!(
                "event: {}\ndata: {event}\n\n",
                event["type"].as_str().unwrap()
            )
        })
        .collect()
}

fn terminal(kind: &str, response: Value) -> Value {
    json!({"type": kind, "response": response})
}

#[tokio::test]
async fn reasoning_summary_and_two_calls_assemble() {
    let turn = assemble(run_ok(TOOLS).await).unwrap();
    assert_eq!(turn.model, "gpt-5.5-2026-08-01");
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let ContentBlock::Thinking(thinking) = &turn.message.content[0] else {
        panic!("{:?}", turn.message.content);
    };
    // A new summary part starts a new paragraph.
    assert_eq!(
        thinking.thinking,
        "**Planning** two lookups.\n\nCalling now."
    );
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls[0].id, "call_hue");
    assert_eq!(calls[0].input, *json!({"city": "Hue"}).as_object().unwrap());
    // No argument deltas: the done item's arguments are used.
    assert_eq!(
        (calls[1].id.as_str(), calls[1].name.as_str()),
        ("call_now", "get_time")
    );
    assert_eq!(calls[1].input, *json!({"zone": "ICT"}).as_object().unwrap());
    assert_eq!(
        turn.usage,
        Usage {
            input_tokens: 200,
            output_tokens: 120,
            cache_read_tokens: 1500,
            cache_creation_tokens: 300,
        }
    );
}

#[tokio::test]
async fn output_items_are_kept_for_replay() {
    let turn = assemble(run_ok(TOOLS).await).unwrap();
    let metadata = turn.message.provider_metadata.expect("metadata");
    let entry = &metadata[METADATA_KEY];
    assert_eq!(entry["response_id"], "resp_01");
    let output = entry["output"].as_array().unwrap();
    assert_eq!(output.len(), 3);
    assert_eq!(output[0]["type"], "reasoning");
    assert_eq!(output[0]["encrypted_content"], "gAAAAB-enc");
    assert_eq!(output[1]["id"], "fc_01");
}

#[tokio::test]
async fn text_turn_with_comment_line_assembles() {
    let turn = assemble(run_ok(TEXT).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert_eq!(turn.message.text(), "It is 31C in Hue.");
    assert_eq!(turn.usage.input_tokens, 40);
}

#[tokio::test]
async fn split_bytes_give_the_same_events() {
    for fixture in [TOOLS, TEXT] {
        let whole: Vec<_> = run(fixture, 1 << 20)
            .await
            .into_iter()
            .map(Result::unwrap)
            .collect();
        let split: Vec<_> = run(fixture, 3)
            .await
            .into_iter()
            .map(Result::unwrap)
            .collect();
        assert_eq!(whole, split);
    }
}

#[tokio::test]
async fn incomplete_reasons_map_to_stop_reasons() {
    for (reason, stop) in [
        ("max_output_tokens", StopReason::MaxTokens),
        ("content_filter", StopReason::Refusal),
    ] {
        let sse = frames(&[
            json!({"type": "response.output_text.delta", "delta": "par"}),
            terminal(
                "response.incomplete",
                json!({"status": "incomplete", "incomplete_details": {"reason": reason}}),
            ),
        ]);
        assert_eq!(assemble(run_ok(&sse).await).unwrap().stop_reason, stop);
    }
    let odd = frames(&[terminal(
        "response.incomplete",
        json!({"status": "incomplete", "incomplete_details": {"reason": "max_messages"}}),
    )]);
    let err = last_error(run(&odd, 64).await);
    assert!(err.to_string().contains("max_messages"), "{err}");
    let cancelled = frames(&[terminal(
        "response.completed",
        json!({"status": "cancelled"}),
    )]);
    assert!(last_error(run(&cancelled, 64).await).retryable());
}

#[tokio::test]
async fn refusal_text_ends_with_refusal() {
    let sse = frames(&[
        json!({"type": "response.refusal.delta", "delta": "I can't help with that."}),
        terminal("response.completed", json!({"status": "completed"})),
    ]);
    let turn = assemble(run_ok(&sse).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::Refusal);
    assert_eq!(turn.message.text(), "I can't help with that.");
    assert_eq!(turn.usage, Usage::default());
}

#[tokio::test]
async fn failed_response_and_error_events_fail_the_call() {
    let failed = frames(&[terminal(
        "response.failed",
        json!({"status": "failed", "error": {"code": "server_error", "message": "The model failed."}}),
    )]);
    let err = last_error(run(&failed, 64).await);
    assert!(
        matches!(&err, ProviderError::Transport { message }
            if message == "response failed: server_error: The model failed."),
        "{err:?}"
    );
    let top = frames(&[
        json!({"type": "error", "code": "ERR_X", "message": "Bearer sk-test-responses bad", "param": null}),
    ]);
    let err = last_error(run(&top, 64).await);
    assert_eq!(
        err.to_string(),
        "provider transport error: stream error: ERR_X: Bearer [redacted] bad"
    );
    let nested = frames(&[json!({"type": "error", "error": {"code": null, "message": "nested"}})]);
    let err = last_error(run(&nested, 64).await);
    assert!(
        err.to_string().ends_with("stream error: unknown: nested"),
        "{err}"
    );
}

#[tokio::test]
async fn an_unfinished_stream_is_a_transport_error() {
    let open = frames(&[json!({"type": "response.output_text.delta", "delta": "hi"})]);
    let err = last_error(run(&open, 64).await);
    assert!(matches!(err, ProviderError::Transport { .. }), "{err}");
    let done = open + "data: [DONE]\n\n";
    assert!(
        last_error(run(&done, 64).await)
            .to_string()
            .contains("[DONE]")
    );
    let err = last_error(run("data: {not json\n\n", 64).await);
    assert!(err.to_string().contains("malformed"), "{err}");
}

#[tokio::test]
async fn a_call_without_ids_is_rejected() {
    let sse = frames(&[json!({
        "type": "response.output_item.added",
        "item": {"type": "function_call", "name": "f", "arguments": ""}
    })]);
    let err = last_error(run(&sse, 64).await);
    assert!(err.to_string().contains("call_id"), "{err}");
}

#[tokio::test]
async fn cancel_mid_stream_ends_with_cancelled() {
    let first = frames(&[json!({"type": "response.output_text.delta", "delta": "hi"})]);
    let bytes = stream::iter([Ok::<_, Infallible>(first.into_bytes())]).chain(stream::pending());
    let cancel = CancellationToken::new();
    let mut events = events(bytes, Translator::new("m".into()), cancel.clone(), key());
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
    let results: Vec<_> = events(bytes(TEXT, 64), Translator::new("m".into()), cancel, key())
        .collect()
        .await;
    assert!(matches!(
        results.as_slice(),
        [Err(ProviderError::Cancelled)]
    ));
}

#[tokio::test]
async fn transport_errors_are_redacted() {
    let bytes = stream::iter([Err::<Vec<u8>, _>(
        "reset; Authorization: Bearer sk-test-responses",
    )]);
    let err = last_error(
        events(
            bytes,
            Translator::new("m".into()),
            CancellationToken::new(),
            key(),
        )
        .collect()
        .await,
    );
    assert!(
        !format!("{err} {err:?}").contains("sk-test-responses"),
        "{err}"
    );
}
