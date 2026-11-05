//! Conversation messages and their content blocks.
//!
//! Every block is tagged by `type` on the wire and rejects unknown fields, so
//! a reader never silently drops data it does not understand.

use crate::json::JsonObject;
use serde::{Deserialize, Serialize};

/// Who authored a message. System text travels separately as [`SystemBlock`]s.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    /// The user, tool results, and runtime notifications.
    User,
    /// The model.
    Assistant,
}

/// One message in a conversation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Message {
    /// Author of the message.
    pub role: Role,
    /// Ordered content blocks.
    pub content: Vec<ContentBlock>,
    /// Opaque provider data that must round-trip with the message, such as
    /// response ids needed to continue a provider-side conversation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_metadata: Option<JsonObject>,
}

impl Message {
    /// A user message holding one text block.
    pub fn user_text(text: impl Into<String>) -> Self {
        Self::new(Role::User, vec![ContentBlock::text(text)])
    }

    /// An assistant message holding one text block.
    pub fn assistant_text(text: impl Into<String>) -> Self {
        Self::new(Role::Assistant, vec![ContentBlock::text(text)])
    }

    /// A message with the given role and blocks and no provider metadata.
    pub fn new(role: Role, content: Vec<ContentBlock>) -> Self {
        Self {
            role,
            content,
            provider_metadata: None,
        }
    }

    /// The tool calls in this message, in order.
    pub fn tool_uses(&self) -> impl Iterator<Item = &ToolUseBlock> {
        self.content.iter().filter_map(|block| match block {
            ContentBlock::ToolUse(call) => Some(call),
            _ => None,
        })
    }

    /// The text blocks of this message joined without separators.
    pub fn text(&self) -> String {
        self.content
            .iter()
            .filter_map(|block| match block {
                ContentBlock::Text(text) => Some(text.text.as_str()),
                _ => None,
            })
            .collect()
    }
}

/// One block of message content.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ContentBlock {
    /// Plain text.
    Text(TextBlock),
    /// An image.
    Image(ImageBlock),
    /// A tool call requested by the model.
    ToolUse(ToolUseBlock),
    /// The result of a tool call, sent back as user content.
    ToolResult(ToolResultBlock),
    /// Visible model reasoning.
    Thinking(ThinkingBlock),
    /// Encrypted model reasoning that must be returned unchanged.
    RedactedThinking(RedactedThinkingBlock),
    /// A runtime notice that the tool, skill, or subagent catalog changed.
    CatalogChange(CatalogChangeBlock),
}

impl ContentBlock {
    /// A text block.
    pub fn text(text: impl Into<String>) -> Self {
        Self::Text(TextBlock { text: text.into() })
    }
}

/// Plain text content.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TextBlock {
    /// The text.
    pub text: String,
}

/// Image content.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImageBlock {
    /// Where the image bytes come from.
    pub source: ImageSource,
}

/// The origin of an image's bytes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ImageSource {
    /// Inline base64 data.
    Base64 {
        /// MIME type, for example `image/png`.
        media_type: String,
        /// Base64-encoded bytes.
        data: String,
    },
    /// A URL the provider fetches.
    Url {
        /// The image URL.
        url: String,
    },
}

/// A tool call requested by the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolUseBlock {
    /// Provider-assigned call id, unique within the response.
    pub id: String,
    /// Name of the requested tool.
    pub name: String,
    /// Tool arguments as a JSON object.
    pub input: JsonObject,
}

/// The result of one tool call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolResultBlock {
    /// The [`ToolUseBlock::id`] this result answers.
    pub tool_use_id: String,
    /// Result content shown to the model.
    pub content: Vec<ToolResultPart>,
    /// Whether the call failed.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub is_error: bool,
}

impl ToolResultBlock {
    /// A result holding one text part.
    pub fn text(tool_use_id: impl Into<String>, text: impl Into<String>, is_error: bool) -> Self {
        Self {
            tool_use_id: tool_use_id.into(),
            content: vec![ToolResultPart::Text(TextBlock { text: text.into() })],
            is_error,
        }
    }
}

/// One part of a tool result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ToolResultPart {
    /// Text output.
    Text(TextBlock),
    /// Image output.
    Image(ImageBlock),
}

/// Visible model reasoning.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThinkingBlock {
    /// The reasoning text.
    pub thinking: String,
    /// Provider signature that authenticates the reasoning on replay.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
}

/// Encrypted reasoning returned unchanged to the provider.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RedactedThinkingBlock {
    /// Opaque provider data.
    pub data: String,
}

/// A runtime notice that the catalog offered to the model changed.
///
/// It travels as user content so every provider sees it the same way.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogChangeBlock {
    /// Model-visible notice text.
    pub text: String,
    /// Names of tools added since the last notice.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub added_tools: Vec<String>,
    /// Names of tools removed since the last notice.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub removed_tools: Vec<String>,
    /// Wire definitions of the added tools.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_definitions: Vec<JsonObject>,
}

/// One block of system prompt text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SystemBlock {
    /// The text.
    pub text: String,
    /// Whether the prompt prefix ending at this block may be cached.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub cacheable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn blocks_are_tagged_and_round_trip() {
        let message = Message::new(
            Role::Assistant,
            vec![
                ContentBlock::text("hi"),
                ContentBlock::ToolUse(ToolUseBlock {
                    id: "c1".into(),
                    name: "Read".into(),
                    input: json!({"path": "a"}).as_object().unwrap().clone(),
                }),
            ],
        );
        let wire = serde_json::to_value(&message).unwrap();
        assert_eq!(
            wire,
            json!({"role": "assistant", "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "c1", "name": "Read", "input": {"path": "a"}}
            ]})
        );
        assert_eq!(serde_json::from_value::<Message>(wire).unwrap(), message);
        assert_eq!(message.tool_uses().count(), 1);
        assert_eq!(message.text(), "hi");
    }

    #[test]
    fn readers_reject_unknown_fields_and_types() {
        for bad in [
            json!({"type": "text", "text": "x", "extra": 1}),
            json!({"type": "video", "url": "x"}),
            json!({"type": "image", "source": {"type": "base64", "data": "x"}}),
            json!({"type": "tool_use", "id": "c", "name": "n", "input": []}),
        ] {
            assert!(
                serde_json::from_value::<ContentBlock>(bad.clone()).is_err(),
                "{bad}"
            );
        }
        assert!(
            serde_json::from_value::<Message>(json!({"role": "system", "content": []})).is_err()
        );
    }

    #[test]
    fn tool_result_omits_false_error_flag() {
        let ok = ToolResultBlock::text("c1", "done", false);
        assert_eq!(
            serde_json::to_value(&ok).unwrap(),
            json!({"tool_use_id": "c1", "content": [{"type": "text", "text": "done"}]})
        );
        let failed: ToolResultBlock =
            serde_json::from_value(json!({"tool_use_id": "c1", "content": [], "is_error": true}))
                .unwrap();
        assert!(failed.is_error);
    }
}
