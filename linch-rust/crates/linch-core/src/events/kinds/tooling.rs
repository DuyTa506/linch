//! Tool invocation kinds: start, end, transient progress, and permission
//! requests.

use crate::json::{JsonObject, JsonValue};
use crate::message::ToolResultBlock;
use serde::{Deserialize, Deserializer, Serialize};

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
    /// `None` is an absent field: no canonical output. `Some(JsonValue::Null)`
    /// is a present field whose value is JSON `null`: the tool had a
    /// canonical output, and that output is `null`. `deserialize_canonical_output`
    /// keeps these distinct; serde's built-in `Option<T>` deserialization
    /// would otherwise collapse a present `null` to `None`, the same as the
    /// field being absent.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "deserialize_canonical_output"
    )]
    pub canonical_output: Option<JsonValue>,
    /// The host-facing [`crate::ToolOutput::metadata`] of a successful
    /// result, after `post_tool_use`; never shown to the model. `None`, and
    /// absent on the wire, for an error or a tool that set none. Boxed so
    /// the tool end is not the largest event every envelope pays for.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Box<JsonObject>>,
    /// The [`crate::ToolOutputError::code`] of a failed call, after
    /// `post_tool_use`; `None` for a success or an error without a code.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
}

/// Decode a present `canonical_output` field as `Some`, including when its
/// value is JSON `null`.
///
/// `#[serde(default, deserialize_with = "...")]` only calls this when the
/// field key is present on the wire; a missing key uses `default`
/// (`Option::default()`, i.e. `None`) without calling it at all. So the
/// three wire states (absent, present-and-null, present-and-a-value) map
/// one-to-one onto `None`, `Some(JsonValue::Null)`, and `Some(other)`,
/// instead of serde's usual `Option<T>` behavior of collapsing a present
/// `null` into `None`.
fn deserialize_canonical_output<'de, D>(deserializer: D) -> Result<Option<JsonValue>, D::Error>
where
    D: Deserializer<'de>,
{
    JsonValue::deserialize(deserializer).map(Some)
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
            metadata: None,
            error_code: None,
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

    fn end_with(canonical_output: Option<JsonValue>) -> ToolCallEndEvent {
        ToolCallEndEvent {
            tool_name: "Read".into(),
            result: ToolResultBlock::text("c1", "done", false),
            duration_ms: 12,
            canonical_output,
            metadata: None,
            error_code: None,
        }
    }

    #[test]
    fn metadata_is_on_the_wire_only_when_set() {
        let mut end = end_with(None);
        let value = serde_json::to_value(&end).unwrap();
        assert!(value.get("metadata").is_none(), "empty metadata is absent");
        let mut rows = JsonObject::new();
        rows.insert("rows".into(), json!(3));
        end.metadata = Some(Box::new(rows));
        let value = serde_json::to_value(&end).unwrap();
        assert_eq!(value["metadata"], json!({"rows": 3}));
        assert_eq!(
            serde_json::from_value::<ToolCallEndEvent>(value).unwrap(),
            end
        );
    }

    #[test]
    fn absent_canonical_output_omits_the_field() {
        let value = serde_json::to_value(end_with(None)).unwrap();
        assert!(value.as_object().unwrap().get("canonical_output").is_none());
        assert_eq!(
            serde_json::from_value::<ToolCallEndEvent>(value).unwrap(),
            end_with(None)
        );
    }

    #[test]
    fn canonical_output_of_json_null_round_trips_distinct_from_absent() {
        // A present `canonical_output: null` must decode to
        // `Some(JsonValue::Null)`, not `None` - the bug a plain
        // `Option<JsonValue>` field would have.
        let value = serde_json::to_value(end_with(Some(JsonValue::Null))).unwrap();
        assert_eq!(value.get("canonical_output"), Some(&JsonValue::Null));
        assert_eq!(
            serde_json::from_value::<ToolCallEndEvent>(value).unwrap(),
            end_with(Some(JsonValue::Null))
        );
    }
}
