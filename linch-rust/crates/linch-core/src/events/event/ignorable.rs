//! The known-type table and the [`IgnorableEvent`] pass-through sentinel.

use crate::events::envelope::EventError;
use crate::json::{JsonObject, JsonValue};

/// Every wire `type` tag a build of this crate recognizes, excluding the
/// open-ended `ignorable` fallback.
pub(super) const KNOWN_EVENT_TYPES: &[&str] = &[
    "init",
    "user",
    "assistant",
    "partial_assistant",
    "usage",
    "tool_call_start",
    "tool_call_end",
    "tool_progress",
    "permission_request",
    "input_request",
    "suspended",
    "context_build",
    "compaction",
    "loop_guard",
    "budget",
    "prompt_cache_advisory",
    "skills_loaded",
    "skill_invoked",
    "skill_completed",
    "subagent",
    "child_run_status",
    "workflow",
    "verification",
    "error",
    "result",
    "model_call_delivery",
    "model_call_receipt",
    "model_call_usage_settlement",
];

/// A durable event of a type this build does not know, kept for
/// pass-through.
///
/// Decoding a `type` this build does not recognize fails unless the
/// enclosing [`crate::events::Envelope`] (or, nested, the enclosing
/// `subagent`'s envelope context) is marked `ignorable`; in that case the
/// event round-trips verbatim as this sentinel rather than aborting the
/// whole decode. Ignorability is a producer-declared compatibility
/// property, not a fallback for a malformed *known* event — see
/// `docs/architecture/events.md`.
#[derive(Debug, Clone, PartialEq)]
pub struct IgnorableEvent {
    type_name: String,
    raw: JsonObject,
}

impl IgnorableEvent {
    /// Build an ignorable event from its wire type and fields.
    ///
    /// Args:
    ///     type_name: The wire `type` tag this build does not recognize.
    ///     raw: The event's other fields (without `type`; it is added).
    ///
    /// Returns:
    ///     The constructed sentinel.
    ///
    /// Raises:
    ///     EventError: If `type_name` is empty or is one this build already
    ///         knows (it would not be safe to treat as opaque).
    pub fn new(type_name: impl Into<String>, mut raw: JsonObject) -> Result<Self, EventError> {
        let type_name = type_name.into();
        if type_name.is_empty() {
            return Err(EventError::EmptyIgnorableType);
        }
        if KNOWN_EVENT_TYPES.contains(&type_name.as_str()) {
            return Err(EventError::KnownTypeIgnorable(type_name));
        }
        raw.insert("type".to_string(), JsonValue::String(type_name.clone()));
        Ok(Self { type_name, raw })
    }

    /// Build a sentinel from a decoder that has already proven `type_name`
    /// is unknown, non-empty, and that `raw` already contains `"type"`.
    pub(super) fn new_trusted(type_name: String, raw: JsonObject) -> Self {
        Self { type_name, raw }
    }

    /// The original wire type, for generic consumers such as reports.
    pub fn type_name(&self) -> &str {
        &self.type_name
    }

    /// The original event envelope, including its `type` field, preserved
    /// verbatim for re-serialization.
    pub fn raw(&self) -> &JsonObject {
        &self.raw
    }
}
