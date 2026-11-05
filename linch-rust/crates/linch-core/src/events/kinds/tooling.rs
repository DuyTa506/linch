//! Tool invocation kinds: start, end, transient progress, and permission
//! requests.

use crate::json::{JsonObject, JsonValue};
use crate::message::ToolResultBlock;
use serde::{Deserialize, Serialize};

/// The start of one tool invocation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallStartEvent {
    /// Identifier assigned to the tool call.
    pub tool_use_id: String,
    /// Name of the tool being invoked.
    pub tool_name: String,
    /// JSON-compatible input supplied to the tool.
    pub input: JsonObject,
    /// Human-readable summary of the invocation.
    pub summary: String,
}

/// The completed result of one tool invocation.
///
/// `result` is a [`ToolResultBlock`], so `tool_use_id` and `is_error` live
/// there rather than being duplicated on this event; use
/// [`ToolCallEndEvent::tool_use_id`] / [`ToolCallEndEvent::is_error`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallEndEvent {
    /// Name of the invoked tool.
    pub tool_name: String,
    /// Result content sent back to the model.
    pub result: ToolResultBlock,
    /// Elapsed invocation time in milliseconds.
    pub duration_ms: u64,
    /// Canonical JSON projection of a structured tool output, when the tool
    /// returned one.
    ///
    /// `Some(JsonValue::Null)` is not representable on the wire: it encodes
    /// to the same `null` as an absent field and always decodes back as
    /// `None`. A producer with a canonical output of JSON `null` has no way
    /// to distinguish that from "no canonical output" over this field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub canonical_output: Option<JsonValue>,
}

impl ToolCallEndEvent {
    /// The [`ToolCallStartEvent::tool_use_id`] this result answers.
    pub fn tool_use_id(&self) -> &str {
        &self.result.tool_use_id
    }

    /// Whether the invocation failed.
    pub fn is_error(&self) -> bool {
        self.result.is_error
    }
}

/// Transient progress reported by an in-flight tool invocation.
///
/// Progress is observational only: it is never added to provider history
/// and does not alter the final [`ToolCallEndEvent`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolProgressEvent {
    /// Identifier assigned to the tool call.
    pub tool_use_id: String,
    /// Name of the in-flight tool.
    pub tool_name: String,
    /// Human-readable progress message.
    pub message: String,
    /// Optional observer payload; contents are not validated.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<JsonObject>,
}

/// One tool call awaiting permission.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionRequestItem {
    /// Identifier assigned to the pending tool call.
    pub tool_use_id: String,
    /// Name of the tool requesting permission.
    pub tool_name: String,
    /// JSON-compatible input proposed for the tool.
    pub input: JsonObject,
    /// Human-readable summary shown to the approver.
    pub summary: String,
}

/// A batch of tool calls awaiting permission.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PermissionRequestEvent {
    /// Pending tool-call descriptions.
    pub requests: Vec<PermissionRequestItem>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn tool_call_end_reads_through_to_its_result_block() {
        let end = ToolCallEndEvent {
            tool_name: "Read".into(),
            result: ToolResultBlock::text("c1", "done", false),
            duration_ms: 12,
            canonical_output: None,
        };
        assert_eq!(end.tool_use_id(), "c1");
        assert!(!end.is_error());
        assert_eq!(
            serde_json::to_value(&end).unwrap(),
            json!({
                "tool_name": "Read",
                "result": {"tool_use_id": "c1", "content": [{"type": "text", "text": "done"}]},
                "duration_ms": 12,
            })
        );
    }
}
