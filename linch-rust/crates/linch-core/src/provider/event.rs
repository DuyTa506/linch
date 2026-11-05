//! The normalized provider event stream.
//!
//! An adapter translates a vendor SDK's response into this fixed vocabulary;
//! [`crate::provider::Assembler`] is the only consumer. Every event is
//! tagged by `type` on the wire and rejects unknown fields, so an adapter
//! that emits something the assembler doesn't expect fails loudly instead of
//! being silently dropped.

use serde::{Deserialize, Serialize};

use crate::json::JsonObject;
use crate::message::RedactedThinkingBlock;
use crate::run::StopReason;
use crate::usage::Usage;

/// One event in a provider's normalized response stream.
///
/// A valid stream is exactly one [`ProviderEvent::MessageStart`] followed by
/// zero or more content events, followed by exactly one
/// [`ProviderEvent::MessageEnd`]; see [`crate::provider::Assembler`] for the
/// full contract.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ProviderEvent {
    /// Opens the stream and names the model that actually served the call.
    MessageStart(MessageStartEvent),
    /// A chunk of assistant-visible text.
    TextDelta(TextDeltaEvent),
    /// A chunk of visible model reasoning.
    ThinkingDelta(ThinkingDeltaEvent),
    /// One complete block of encrypted model reasoning.
    RedactedThinking(RedactedThinkingBlock),
    /// Opens a tool call; its id and name are fixed for the call's lifetime.
    ToolUseStart(ToolUseStartEvent),
    /// A chunk of a tool call's JSON input, for a call opened and not yet ended.
    ToolUseInputDelta(ToolUseInputDeltaEvent),
    /// Closes a tool call; its accumulated input must parse as a JSON object.
    ToolUseEnd(ToolUseEndEvent),
    /// Closes the stream with the response's stop reason and usage.
    MessageEnd(MessageEndEvent),
}

impl ProviderEvent {
    /// The wire `type` tag of this event, for error messages and logging.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::MessageStart(_) => "message_start",
            Self::TextDelta(_) => "text_delta",
            Self::ThinkingDelta(_) => "thinking_delta",
            Self::RedactedThinking(_) => "redacted_thinking",
            Self::ToolUseStart(_) => "tool_use_start",
            Self::ToolUseInputDelta(_) => "tool_use_input_delta",
            Self::ToolUseEnd(_) => "tool_use_end",
            Self::MessageEnd(_) => "message_end",
        }
    }
}

/// Payload of [`ProviderEvent::MessageStart`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MessageStartEvent {
    /// Model that actually served the call (may differ from the request
    /// after a fallback).
    pub model: String,
}

/// Payload of [`ProviderEvent::TextDelta`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TextDeltaEvent {
    /// The text chunk.
    pub text: String,
}

/// Payload of [`ProviderEvent::ThinkingDelta`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThinkingDeltaEvent {
    /// The reasoning text chunk.
    pub text: String,
    /// Provider signature covering the reasoning emitted so far, when the
    /// provider sends one. The assembler keeps the last signature seen in a
    /// run of deltas.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
}

/// Payload of [`ProviderEvent::ToolUseStart`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolUseStartEvent {
    /// Provider-assigned call id, unique for the whole stream.
    pub id: String,
    /// Name of the requested tool.
    pub name: String,
}

/// Payload of [`ProviderEvent::ToolUseInputDelta`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolUseInputDeltaEvent {
    /// Id of the open call this chunk belongs to.
    pub id: String,
    /// A chunk of the call's JSON input, concatenated in order.
    pub json_delta: String,
}

/// Payload of [`ProviderEvent::ToolUseEnd`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolUseEndEvent {
    /// Id of the call being closed.
    pub id: String,
}

/// Payload of [`ProviderEvent::MessageEnd`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MessageEndEvent {
    /// Why the response stopped.
    pub stop_reason: StopReason,
    /// Token usage reported for the response.
    pub usage: Usage,
    /// Opaque provider data carried onto the assembled message's
    /// `provider_metadata`, such as a response id needed to continue a
    /// provider-side conversation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_metadata: Option<JsonObject>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn events_are_tagged_and_round_trip() {
        let event = ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: "c1".into(),
            name: "Read".into(),
        });
        let wire = serde_json::to_value(&event).unwrap();
        assert_eq!(
            wire,
            json!({"type": "tool_use_start", "id": "c1", "name": "Read"})
        );
        assert_eq!(
            serde_json::from_value::<ProviderEvent>(wire).unwrap(),
            event
        );
        assert_eq!(event.kind(), "tool_use_start");
    }

    #[test]
    fn message_end_omits_absent_metadata_and_round_trips() {
        let event = ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: StopReason::EndTurn,
            usage: Usage::default(),
            provider_metadata: None,
        });
        let wire = serde_json::to_value(&event).unwrap();
        assert_eq!(
            wire,
            json!({
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0
                }
            })
        );
        assert_eq!(
            serde_json::from_value::<MessageEndEvent>(json!({
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1}
            }))
            .unwrap()
            .provider_metadata,
            None
        );
    }

    #[test]
    fn readers_reject_unknown_fields_and_tags() {
        for bad in [
            json!({"type": "text_delta", "text": "x", "extra": 1}),
            json!({"type": "tool_use_start", "id": "", "extra": 1}),
            json!({"type": "sound_delta", "text": "x"}),
            json!({"type": "message_end", "stop_reason": "paused", "usage": {}}),
        ] {
            assert!(
                serde_json::from_value::<ProviderEvent>(bad.clone()).is_err(),
                "{bad}"
            );
        }
    }
}
