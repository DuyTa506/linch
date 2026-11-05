//! Assembler contract tests: a proptest oracle for valid streams here, one
//! test per contract rule violation in [`rules`], and a few pinned
//! coalescing edge cases in [`coalescing`].

use proptest::prelude::*;

use super::*;
use crate::json::JsonValue;
use crate::message::{RedactedThinkingBlock, Role};
use crate::provider::event::{
    MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent, ToolUseInputDeltaEvent,
    ToolUseStartEvent,
};

mod coalescing;
mod rules;

// --- a small recursive JSON strategy (null/bool/i64/string/array/object; no
// floats -- nothing in the contract depends on float formatting) ---------

fn json_leaf() -> impl Strategy<Value = JsonValue> {
    prop_oneof![
        Just(JsonValue::Null),
        any::<bool>().prop_map(JsonValue::Bool),
        any::<i64>().prop_map(|n| JsonValue::Number(n.into())),
        "[a-z]{0,6}".prop_map(JsonValue::String),
    ]
}

fn json_value(depth: u32) -> BoxedStrategy<JsonValue> {
    if depth == 0 {
        json_leaf().boxed()
    } else {
        prop_oneof![
            2 => json_leaf(),
            1 => prop::collection::vec(json_value(depth - 1), 0..3).prop_map(JsonValue::Array),
            1 => prop::collection::vec(("[a-z]{1,6}", json_value(depth - 1)), 0..3)
                .prop_map(|pairs| JsonValue::Object(pairs.into_iter().collect())),
        ]
        .boxed()
    }
}

fn json_object(depth: u32) -> impl Strategy<Value = JsonObject> {
    prop::collection::vec(("[a-z]{1,6}", json_value(depth)), 0..4)
        .prop_map(|pairs| pairs.into_iter().collect())
}

// --- content seeds: one stream event each, composed into a full stream ---

#[derive(Debug, Clone)]
enum Seed {
    Text(String),
    Thinking(String, Option<String>),
    Redacted(String),
    ToolUse(String, JsonObject),
}

fn seed_strategy() -> impl Strategy<Value = Seed> {
    prop_oneof![
        3 => "[a-zA-Z0-9 ]{1,8}".prop_map(Seed::Text),
        // 0-length text is included: a thinking_delta with empty text but a
        // signature is valid provider output (see `Assembler::flush_thinking`).
        3 => ("[a-zA-Z0-9 ]{0,8}", prop::option::of("[a-z]{1,6}"))
            .prop_map(|(text, sig)| Seed::Thinking(text, sig)),
        1 => "[a-z]{1,6}".prop_map(Seed::Redacted),
        2 => ("[A-Za-z]{1,8}", json_object(2)).prop_map(|(name, input)| Seed::ToolUse(name, input)),
    ]
}

/// Model state for [`events_and_expected`], tracking the same two pending
/// runs [`Assembler`] does, flushed at the same points, so a kind-switching
/// event that itself produces no block (an empty, unsigned `thinking_delta`)
/// still correctly ends whatever run preceded it.
#[derive(Default)]
struct ExpectedContent {
    blocks: Vec<ContentBlock>,
    text: String,
    thinking: String,
    thinking_sig: Option<String>,
}

impl ExpectedContent {
    fn flush_text(&mut self) {
        if !self.text.is_empty() {
            self.blocks.push(ContentBlock::Text(TextBlock {
                text: std::mem::take(&mut self.text),
            }));
        }
    }

    fn flush_thinking(&mut self) {
        let signature = self.thinking_sig.take();
        if !self.thinking.is_empty() || signature.is_some() {
            self.blocks.push(ContentBlock::Thinking(ThinkingBlock {
                thinking: std::mem::take(&mut self.thinking),
                signature,
            }));
        }
    }
}

/// Turn seeds into the event stream that would produce them and the
/// `content` the assembler should coalesce them into, mirroring the
/// coalescing rules documented on [`Assembler`].
fn events_and_expected(model: &str, seeds: &[Seed]) -> (Vec<ProviderEvent>, Vec<ContentBlock>) {
    let mut events = vec![ProviderEvent::MessageStart(MessageStartEvent {
        model: model.to_string(),
    })];
    let mut expected = ExpectedContent::default();
    for (index, seed) in seeds.iter().enumerate() {
        match seed {
            Seed::Text(text) => {
                events.push(ProviderEvent::TextDelta(TextDeltaEvent {
                    text: text.clone(),
                }));
                expected.flush_thinking();
                expected.text.push_str(text);
            }
            Seed::Thinking(text, signature) => {
                events.push(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
                    text: text.clone(),
                    signature: signature.clone(),
                }));
                expected.flush_text();
                expected.thinking.push_str(text);
                if let Some(sig) = signature {
                    expected.thinking_sig = Some(sig.clone());
                }
            }
            Seed::Redacted(data) => {
                events.push(ProviderEvent::RedactedThinking(RedactedThinkingBlock {
                    data: data.clone(),
                }));
                expected.flush_text();
                expected.flush_thinking();
                expected
                    .blocks
                    .push(ContentBlock::RedactedThinking(RedactedThinkingBlock {
                        data: data.clone(),
                    }));
            }
            Seed::ToolUse(name, input) => {
                let id = format!("call-{index}");
                events.push(ProviderEvent::ToolUseStart(ToolUseStartEvent {
                    id: id.clone(),
                    name: name.clone(),
                }));
                let raw = serde_json::to_string(input).expect("object always serializes");
                events.push(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                    id: id.clone(),
                    json_delta: raw,
                }));
                events.push(ProviderEvent::ToolUseEnd(ToolUseEndEvent {
                    id: id.clone(),
                }));
                expected.flush_text();
                expected.flush_thinking();
                expected.blocks.push(ContentBlock::ToolUse(ToolUseBlock {
                    id,
                    name: name.clone(),
                    input: input.clone(),
                }));
            }
        }
    }
    expected.flush_text();
    expected.flush_thinking();
    (events, expected.blocks)
}

fn non_tool_use_stop_reason() -> impl Strategy<Value = StopReason> {
    prop_oneof![
        Just(StopReason::EndTurn),
        Just(StopReason::MaxTokens),
        Just(StopReason::StopSequence),
        Just(StopReason::Refusal),
        Just(StopReason::Error),
        Just(StopReason::LeaseLost),
        Just(StopReason::Interrupted),
    ]
}

/// A `message_start` event for the given model, shared by every test
/// submodule.
fn start(model: &str) -> ProviderEvent {
    ProviderEvent::MessageStart(MessageStartEvent {
        model: model.into(),
    })
}

/// A `message_end` event with the given stop reason and default usage,
/// shared by every test submodule.
fn end(stop_reason: StopReason) -> ProviderEvent {
    ProviderEvent::MessageEnd(MessageEndEvent {
        stop_reason,
        usage: Usage::default(),
        provider_metadata: None,
    })
}

proptest! {
    /// Any stream built from [`events_and_expected`] assembles to exactly
    /// the content it was built from, and every event in it round-trips
    /// through the wire.
    #[test]
    fn valid_streams_assemble_to_the_expected_message(
        model in "[a-zA-Z0-9_.-]{1,12}",
        seeds in prop::collection::vec(seed_strategy(), 0..8),
        other_stop_reason in non_tool_use_stop_reason(),
        usage_buckets in any::<[u64; 4]>(),
    ) {
        let (mut events, expected) = events_and_expected(&model, &seeds);
        let has_tool_use = expected.iter().any(|block| matches!(block, ContentBlock::ToolUse(_)));
        let stop_reason = if has_tool_use { StopReason::ToolUse } else { other_stop_reason };
        let usage = Usage {
            input_tokens: usage_buckets[0],
            output_tokens: usage_buckets[1],
            cache_read_tokens: usage_buckets[2],
            cache_creation_tokens: usage_buckets[3],
        };
        events.push(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason,
            usage,
            provider_metadata: None,
        }));

        for event in &events {
            let wire = serde_json::to_value(event).unwrap();
            prop_assert_eq!(&serde_json::from_value::<ProviderEvent>(wire).unwrap(), event);
        }

        let assembly = assemble(events).expect("a stream built this way must be valid");
        prop_assert_eq!(assembly.model, model);
        prop_assert_eq!(assembly.stop_reason, stop_reason);
        prop_assert_eq!(assembly.usage, usage);
        prop_assert_eq!(assembly.message.role, Role::Assistant);
        prop_assert_eq!(assembly.message.content, expected);
        prop_assert_eq!(assembly.message.provider_metadata, None);
    }
}
