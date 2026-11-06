//! Run lifecycle kinds: init, user/assistant messages, error, and the
//! terminal result.

use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::message::Message;
use crate::run::StopReason;
use crate::usage::Usage;
use serde::{Deserialize, Serialize};

/// Run initialization: the session, run, model, and tools offered.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InitEvent {
    /// Session that owns the run.
    pub session_id: SessionId,
    /// Run being initialized.
    pub run_id: RunId,
    /// Parent run, if this run is a subagent child.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_run_id: Option<RunId>,
    /// Model selected for the run.
    pub model: String,
    /// Names of tools offered to the model.
    pub tools: Vec<String>,
    /// Trace identifier correlating this run with host observability.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

/// Why a [`UserEvent`] was emitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UserEventSubtype {
    /// A user-authored prompt.
    Prompt,
    /// A tool result returned to the model as user content.
    ToolResult,
    /// A host-injected alignment message.
    Alignment,
    /// A host-injected runtime notification.
    Notification,
}

/// A user-authored or user-visible message.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserEvent {
    /// Message carried by the event.
    pub message: Message,
    /// Kind of user event represented.
    pub subtype: UserEventSubtype,
}

/// A completed assistant response.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssistantEvent {
    /// Assistant message returned by the provider.
    pub message: Message,
    /// Reason the provider stopped generating.
    pub stop_reason: StopReason,
}

/// A transient assistant delta while a response streams.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PartialAssistantEvent {
    /// Provider-normalized fields emitted for the current stream update.
    pub delta: JsonObject,
}

/// A terminal or recoverable error payload.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorEvent {
    /// Structured error details.
    pub error: JsonObject,
}

/// Terminal outcome category of a [`ResultEvent`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultSubtype {
    /// The run completed successfully.
    Success,
    /// The run failed with an error.
    Error,
    /// The run was aborted by request.
    Aborted,
    /// The run was interrupted before completion.
    Interrupted,
}

/// The terminal outcome of a run.
///
/// A suspended run's stream ends with a
/// [`SuspendedEvent`](super::SuspendedEvent) instead of this.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResultEvent {
    /// Terminal outcome category.
    pub subtype: ResultSubtype,
    /// Reason the loop stopped.
    pub stop_reason: StopReason,
    /// Usage summed across every turn whose usage is known.
    pub total_usage: Usage,
    /// Whether `total_usage` accounts for every turn. `false` means at
    /// least one turn's usage was unknown and is excluded from the sum
    /// (never folded in as a zero [`Usage`]).
    pub usage_complete: bool,
    /// Wall time in milliseconds of the call that ended the run (the run,
    /// or the resume or approve that continued it), from its start to this
    /// result. The whole run's span is this envelope's `ts_ms` minus the
    /// `init` envelope's.
    pub duration_ms: u64,
    /// Optional final textual answer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_text: Option<String>,
    /// Parsed structured answer, when an output schema was configured.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_output: Option<JsonObject>,
    /// Structured-output parsing or validation error, when it failed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_error: Option<String>,
    /// Trace identifier associated with the run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unknown_fields_are_rejected() {
        assert!(serde_json::from_value::<ErrorEvent>(json!({"error": {}, "extra": 1})).is_err());
    }

    fn result_value(
        total_usage: serde_json::Value,
        usage_complete: serde_json::Value,
    ) -> serde_json::Value {
        json!({
            "subtype": "success",
            "stop_reason": "end_turn",
            "total_usage": total_usage,
            "usage_complete": usage_complete,
            "duration_ms": 12,
        })
    }

    #[test]
    fn result_event_requires_total_usage_usage_complete_and_duration_ms() {
        // None of these fields has a default: a run whose usage accounting
        // is incomplete must say so explicitly, and a missing field is a
        // decode error rather than a silently-assumed default.
        let zero = json!({
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        });
        let full = result_value(zero, json!(true));
        for field in ["total_usage", "usage_complete", "duration_ms"] {
            let mut missing = full.clone();
            missing.as_object_mut().unwrap().remove(field);
            let decoded = serde_json::from_value::<ResultEvent>(missing);
            assert!(decoded.is_err(), "{field} is required");
        }
        assert!(serde_json::from_value::<ResultEvent>(full).is_ok());
    }

    #[test]
    fn result_event_total_usage_and_usage_complete_round_trip() {
        let zero_usage = json!({
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        });
        let value = result_value(zero_usage, json!(false));
        let event: ResultEvent = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(event.total_usage, Usage::default());
        assert!(!event.usage_complete);
        assert_eq!(serde_json::to_value(&event).unwrap(), value);
    }
}
