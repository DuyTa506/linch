use std::collections::HashSet;
use std::convert::Infallible;

use futures::{StreamExt, stream};
use linch_core::{
    ContentBlock, ProviderError, ProviderEvent, StopReason, ThinkingBlock, Usage, assemble,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use super::{Translator, events};
use crate::ApiKey;
use crate::gemini::PARTS_KEY;

const TEXT: &str = include_str!("../../../tests/fixtures/gemini_text_thinking.sse");
const CALLS: &str = include_str!("../../../tests/fixtures/gemini_parallel_calls.sse");

fn key() -> ApiKey {
    ApiKey::new("AIza-test").unwrap()
}

/// Run `sse` through the driver, split into `chunk`-byte pieces. The call
/// id prefix is fixed so that runs compare equal.
async fn run(sse: &str, chunk: usize) -> Vec<Result<ProviderEvent, ProviderError>> {
    let pieces: Vec<Result<Vec<u8>, Infallible>> = sse
        .as_bytes()
        .chunks(chunk)
        .map(|piece| Ok(piece.to_vec()))
        .collect();
    let mut translator = Translator::new("gemini-requested".into(), key());
    translator.call_prefix = "gemini_t".into();
    events(
        stream::iter(pieces),
        translator,
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

fn frames(chunks: &[Value]) -> String {
    chunks
        .iter()
        .map(|chunk| format!("data: {chunk}\n\n"))
        .collect()
}

fn candidate(parts: Value, finish: Option<&str>) -> Value {
    let mut candidate = json!({"content": {"parts": parts, "role": "model"}});
    if let Some(finish) = finish {
        candidate["finishReason"] = finish.into();
    }
    json!({"candidates": [candidate]})
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
async fn text_turn_keeps_thoughts_text_usage_and_the_signed_parts() {
    let turn = assemble(run_ok(TEXT).await).unwrap();
    assert_eq!(turn.model, "gemini-3.8-flash");
    assert_eq!(turn.stop_reason, StopReason::EndTurn);
    assert_eq!(
        turn.message.content,
        vec![
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "**Planning** the reply.".into(),
                signature: None,
            }),
            ContentBlock::text("Pong"),
        ]
    );
    assert_eq!(
        turn.usage.unwrap(),
        Usage {
            input_tokens: 5,
            output_tokens: 22,
            cache_read_tokens: 4,
            cache_creation_tokens: 0,
        }
    );
    let metadata = turn.message.provider_metadata.expect("signed turn is kept");
    assert_eq!(
        metadata[PARTS_KEY],
        json!([
            {"text": "**Planning** the reply.", "thought": true},
            {"text": "Pong"},
            {"text": "", "thoughtSignature": "c2lnLXRleHQ="},
        ])
    );
}

#[tokio::test]
async fn parallel_calls_get_numbered_ids_and_keep_the_signature_on_the_first() {
    let turn = assemble(run_ok(CALLS).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::ToolUse);
    let calls: Vec<_> = turn.message.tool_uses().collect();
    assert_eq!(calls[0].id, "gemini_t_0");
    assert_eq!(calls[1].id, "gemini_t_1");
    assert_eq!(calls[0].input["city"], "Paris");
    assert_eq!(calls[1].input["city"], "Tokyo");
    assert_eq!(turn.usage.unwrap().output_tokens, 42);
    let parts = &turn.message.provider_metadata.unwrap()[PARTS_KEY];
    assert_eq!(parts[1]["thoughtSignature"], "c2lnLWNhbGw=");
    assert!(parts[2].get("thoughtSignature").is_none());
    assert!(parts[1]["functionCall"].get("id").is_none());
}

#[tokio::test]
async fn synthesized_ids_are_unique_per_response() {
    // After compaction two responses can follow the same number of turns.
    let mut ids = Vec::new();
    for _ in 0..2 {
        let bytes = stream::iter([Ok::<_, Infallible>(CALLS.as_bytes().to_vec())]);
        let translator = Translator::new("m".into(), key());
        let results: Vec<_> = events(bytes, translator, CancellationToken::new(), key())
            .collect()
            .await;
        let turn = assemble(results.into_iter().map(Result::unwrap)).unwrap();
        ids.extend(turn.message.tool_uses().map(|call| call.id.clone()));
    }
    let unique: HashSet<_> = ids.iter().collect();
    assert_eq!((ids.len(), unique.len()), (4, 4), "{ids:?}");
    // OpenAI caps tool call ids at 40 characters.
    assert!(
        ids.iter()
            .all(|id| id.starts_with("gemini_") && id.len() <= 40),
        "{ids:?}"
    );
}

#[tokio::test]
async fn split_bytes_give_the_same_events() {
    for fixture in [TEXT, CALLS] {
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
async fn server_call_ids_are_used_and_kept() {
    let call = json!([{"functionCall": {"id": "fc-9", "name": "now"}}]);
    let turn = assemble(run_ok(&frames(&[candidate(call, Some("STOP"))])).await).unwrap();
    let call = turn.message.tool_uses().next().unwrap();
    assert_eq!(call.id, "fc-9");
    assert!(call.input.is_empty());
    assert!(turn.message.provider_metadata.is_some());
}

#[tokio::test]
async fn a_last_chunk_with_no_blank_line_after_it_is_read() {
    let sse = frames(&[candidate(json!([{"text": "hi"}]), Some("STOP"))]);
    for end in ["\n", "\r\n", ""] {
        let cut = format!("{}{end}", sse.trim_end());
        let turn = assemble(run_ok(&cut).await).unwrap();
        assert_eq!(
            (turn.message.text().as_str(), turn.stop_reason),
            ("hi", StopReason::EndTurn),
            "{end:?}"
        );
    }
}

#[tokio::test]
async fn unsigned_turns_keep_no_metadata_and_fall_back_to_the_requested_model() {
    let chunk = candidate(json!([{"text": "hi"}]), Some("STOP"));
    let turn = assemble(run_ok(&frames(&[chunk])).await).unwrap();
    assert_eq!(turn.model, "gemini-requested");
    assert!(turn.message.provider_metadata.is_none());
    assert_eq!(turn.usage, None);
}

#[tokio::test]
async fn finish_reasons_map_to_stop_reasons() {
    for (wire, stop) in [
        ("STOP", StopReason::EndTurn),
        ("MAX_TOKENS", StopReason::MaxTokens),
        ("SAFETY", StopReason::Refusal),
        ("RECITATION", StopReason::Refusal),
        ("PROHIBITED_CONTENT", StopReason::Refusal),
        ("SPII", StopReason::Refusal),
    ] {
        let chunk = candidate(json!([{"text": "x"}]), Some(wire));
        let turn = assemble(run_ok(&frames(&[chunk])).await).unwrap();
        assert_eq!(turn.stop_reason, stop, "{wire}");
    }
}

#[tokio::test]
async fn a_blocked_prompt_is_a_refusal_with_no_content() {
    let chunk = json!({"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 7}});
    let turn = assemble(run_ok(&frames(&[chunk])).await).unwrap();
    assert_eq!(turn.stop_reason, StopReason::Refusal);
    assert!(turn.message.content.is_empty());
    assert_eq!(turn.usage.unwrap().input_tokens, 7);
}

#[tokio::test]
async fn abnormal_finishes_fail_the_call() {
    for wire in [
        "MALFORMED_FUNCTION_CALL",
        "OTHER",
        "UNEXPECTED_TOOL_CALL",
        "NEW_REASON",
    ] {
        let chunk = candidate(json!([{"text": "x"}]), Some(wire));
        let err = last_error(run(&frames(&[chunk]), 4096).await);
        assert!(
            matches!(err, ProviderError::Transport { .. }),
            "{wire}: {err}"
        );
    }
    let chunk = candidate(json!([]), Some("MISSING_THOUGHT_SIGNATURE"));
    let err = last_error(run(&frames(&[chunk]), 4096).await);
    assert!(matches!(err, ProviderError::InvalidRequest { .. }), "{err}");
}

#[tokio::test]
async fn an_error_chunk_is_a_status_error() {
    let ok = candidate(json!([{"text": "x"}]), None);
    let error = json!({"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}});
    let err = last_error(run(&frames(&[ok, error]), 4096).await);
    assert!(
        matches!(err, ProviderError::Status { status: 503, .. }),
        "{err:?}"
    );
}

#[tokio::test]
async fn broken_streams_are_transport_errors() {
    let unfinished = candidate(json!([{"text": "x"}]), None);
    for sse in [
        String::new(),
        frames(&[unfinished]),
        "data: {not json\n\n".to_string(),
    ] {
        let err = last_error(run(&sse, 4096).await);
        assert!(
            matches!(err, ProviderError::Transport { .. }),
            "{sse}: {err}"
        );
    }
}

#[tokio::test]
async fn cancel_mid_stream_ends_with_cancelled() {
    let first = frames(&[candidate(json!([{"text": "hi"}]), None)]);
    let bytes = stream::iter([Ok::<_, Infallible>(first.into_bytes())]).chain(stream::pending());
    let cancel = CancellationToken::new();
    let translator = Translator::new("m".into(), key());
    let mut events = events(bytes, translator, cancel.clone(), key());
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
async fn transport_and_stream_errors_are_redacted() {
    let bytes = stream::iter([Err::<Vec<u8>, _>("reset; x-goog-api-key: AIza-test")]);
    let translator = Translator::new("m".into(), key());
    let err = last_error(
        events(bytes, translator, CancellationToken::new(), key())
            .collect()
            .await,
    );
    assert!(!format!("{err} {err:?}").contains("AIza-test"), "{err}");
    let echoed = json!({"error": {"code": 400, "message": "bad key AIza-test"}});
    let err = last_error(run(&frames(&[echoed]), 4096).await);
    assert!(!format!("{err} {err:?}").contains("AIza-test"), "{err}");
}
