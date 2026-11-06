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

fn thinking(text: &str, signature: Option<&str>) -> ProviderEvent {
    ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
        text: text.into(),
        signature: signature.map(Into::into),
    })
}

fn block(text: &str, signature: Option<&str>) -> ContentBlock {
    ContentBlock::Thinking(ThinkingBlock {
        thinking: text.into(),
        signature: signature.map(Into::into),
    })
}

#[test]
fn a_signed_thinking_delta_closes_its_block() {
    let assembly = assemble([
        start("m"),
        thinking("a", Some("sig-1")),
        thinking("b", None),
        thinking("c", Some("sig-2")),
        end(StopReason::EndTurn),
    ])
    .unwrap();
    assert_eq!(
        assembly.message.content,
        [block("a", Some("sig-1")), block("bc", Some("sig-2"))]
    );
}

/// Claude 5.x sends a reasoning block and a separately signed progress
/// update back to back before a tool call (R1-prov 1). Each must replay as
/// it came, or the next request is refused.
#[test]
fn back_to_back_signed_thinking_blocks_stay_apart() {
    let assembly = assemble([
        start("m"),
        thinking("reasoning A", None),
        thinking("", Some("sigA")),
        thinking("update B", None),
        thinking("", Some("sigB")),
        ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        }),
        ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
            id: "c1".into(),
            json_delta: "{}".into(),
        }),
        ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: "c1".into() }),
        end(StopReason::ToolUse),
    ])
    .unwrap();
    let content = &assembly.message.content;
    assert_eq!(
        content[..2],
        [
            block("reasoning A", Some("sigA")),
            block("update B", Some("sigB"))
        ]
    );
    assert!(matches!(content[2], ContentBlock::ToolUse(_)));
    assert_eq!(content.len(), 3);
}

#[test]
fn an_empty_signed_thinking_run_emits_its_signature_and_does_not_leak_it_further() {
    let assembly = assemble([
        start("m"),
        // An empty-text delta carrying a signature is valid provider output:
        // it must still produce a block so the signature round-trips, even
        // though the block's own thinking text is empty.
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "".into(),
            signature: Some("sig-1".into()),
        }),
        // A later, unrelated thinking run must not inherit that signature.
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
            ContentBlock::Thinking(ThinkingBlock {
                thinking: "".into(),
                signature: Some("sig-1".into()),
            }),
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
fn an_empty_unsigned_thinking_run_emits_nothing() {
    let assembly = assemble([
        start("m"),
        ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "".into(),
            signature: None,
        }),
        end(StopReason::EndTurn),
    ])
    .unwrap();
    assert_eq!(assembly.message.content, vec![]);
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
            usage: Some(Usage::default()),
            provider_metadata: Some(metadata.clone()),
        }),
    ])
    .unwrap();
    assert_eq!(assembly.message.provider_metadata, Some(metadata));
}
