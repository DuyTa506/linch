//! Small, typed helpers for hand-written envelope/event decoding.
//!
//! [`crate::events::Event`] and [`crate::events::Envelope`] cannot use plain
//! `#[derive(Deserialize)]` (see their module docs for why), so they parse
//! their few own fields off a [`JsonObject`] directly. These helpers keep
//! that parsing uniform and keep [`super::EventError`] typed rather than
//! stringly.

use super::EventError;
use crate::json::{JsonObject, JsonValue};

/// Consume a JSON value that must be an object.
pub(super) fn take_object(value: JsonValue) -> Result<JsonObject, EventError> {
    match value {
        JsonValue::Object(obj) => Ok(obj),
        other => Err(EventError::NotAnObject(json_kind_name(&other))),
    }
}

/// Remove and return a required string field.
pub(super) fn take_string(obj: &mut JsonObject, field: &'static str) -> Result<String, EventError> {
    match obj.remove(field) {
        Some(JsonValue::String(s)) => Ok(s),
        Some(other) => Err(EventError::InvalidField {
            field,
            expected: "a string",
            found: json_kind_name(&other),
        }),
        None => Err(EventError::MissingField(field)),
    }
}

/// Remove and return a required non-negative integer field.
pub(super) fn take_u64(obj: &mut JsonObject, field: &'static str) -> Result<u64, EventError> {
    match obj.remove(field) {
        Some(JsonValue::Number(n)) => n.as_u64().ok_or(EventError::InvalidField {
            field,
            expected: "a non-negative integer",
            found: "a number outside u64 range",
        }),
        Some(other) => Err(EventError::InvalidField {
            field,
            expected: "a non-negative integer",
            found: json_kind_name(&other),
        }),
        None => Err(EventError::MissingField(field)),
    }
}

/// Remove and return an optional boolean field, defaulting to `false`.
pub(super) fn take_bool_default_false(
    obj: &mut JsonObject,
    field: &'static str,
) -> Result<bool, EventError> {
    match obj.remove(field) {
        None => Ok(false),
        Some(JsonValue::Bool(b)) => Ok(b),
        Some(other) => Err(EventError::InvalidField {
            field,
            expected: "a boolean",
            found: json_kind_name(&other),
        }),
    }
}

/// Fail if any fields remain in `obj` after the expected ones were removed.
pub(super) fn reject_unknown_fields(obj: JsonObject) -> Result<(), EventError> {
    match obj.into_iter().next() {
        Some((key, _)) => Err(EventError::UnknownField(key)),
        None => Ok(()),
    }
}

/// A short, human-readable name for a JSON value's kind, used in error
/// messages.
pub(super) fn json_kind_name(value: &JsonValue) -> &'static str {
    match value {
        JsonValue::Null => "null",
        JsonValue::Bool(_) => "a boolean",
        JsonValue::Number(_) => "a number",
        JsonValue::String(_) => "a string",
        JsonValue::Array(_) => "an array",
        JsonValue::Object(_) => "an object",
    }
}
