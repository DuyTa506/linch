//! Pinned coalescing edge cases that the generative proptest oracle
//! deliberately does not cover (see `super::events_and_expected`'s doc).

use super::*;
use crate::json::JsonValue;

#[test]
fn text_delta_during_an_open_tool_call_lands_after_its_block() {
    let assembly = assemble([
        start("m"),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::TextDelta(TextDeltaEvent {
            text: "aside".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        end(StopReason::ToolUse),
    ])
    .expect("a text delta between tool_use_start and tool_use_end is not itself a violation");
    assert!(matches!(
        assembly.message.content[0],
        ContentBlock::ToolUse(_)
    ));
    assert_eq!(
        assembly.message.content[1],
        ContentBlock::Text(TextBlock {
            text: "aside".into()
        })
    );
}

#[test]
fn thinking_signature_is_the_last_one_seen_in_the_run() {
    let assembly = assemble([
        start("m"),
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "a".into(),
            signature: Some("sig-1".into()),
        }),
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "b".into(),
            signature: None,
        }),
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "c".into(),
            signature: Some("sig-2".into()),
        }),
        end(StopReason::EndTurn),
    ])
    .unwrap();
    assert_eq!(
        assembly.message.content,
        vec![ContentBlock::Thinking(ThinkingBlock {
            thinking: "abc".into(),
            signature: Some("sig-2".into()),
        })]
    );
}

#[test]
fn an_empty_thinking_run_does_not_leak_its_signature_into_the_next_run() {
    let assembly = assemble([
        start("m"),
        // An empty-text delta carrying a signature produces no block...
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "".into(),
            signature: Some("stale".into()),
        }),
        // ...so a later, unrelated thinking run must not inherit it.
        ProviderEvent::TextDelta(TextDeltaEvent {
            text: "between".into(),
        }),
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "fresh".into(),
            signature: None,
        }),
        end(StopReason::EndTurn),
    ])
    .unwrap();
    assert_eq!(
        assembly.message.content,
        vec![
            ContentBlock::Text(TextBlock {
                text: "between".into()
            }),
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "fresh".into(),
                signature: None,
            }),
        ]
    );
}

#[test]
fn message_end_provider_metadata_becomes_the_message_provider_metadata() {
    let metadata: JsonObject = [("response_id".to_string(), JsonValue::String("r1".into()))]
        .into_iter()
        .collect();
    let assembly = assemble([
        start("m"),
        ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: StopReason::EndTurn,
            usage: Usage::default(),
            provider_metadata: Some(metadata.clone()),
        }),
    ])
    .unwrap();
    assert_eq!(assembly.message.provider_metadata, Some(metadata));
}
