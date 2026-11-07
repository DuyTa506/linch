//! Small, typed helpers for hand-written envelope/event decoding, and for
//! the few payload fields that need a stricter encoding than their derive
//! gives them for free.
//!
//! [`crate::events::Event`] and [`crate::events::Envelope`] cannot use plain
//! `#[derive(Deserialize)]` (see their module docs for why), so they parse
//! their few own fields off a [`JsonObject`] directly. These helpers keep
//! that parsing uniform and keep [`super::EventError`] typed rather than
//! stringly. [`serialize_finite_f64`] and [`serialize_finite_f64_option`]
//! are unrelated to that decode path; every monetary `f64`/`Option<f64>`
//! field in [`super::kinds`] and [`super::model_call`] uses one of them via
//! `#[serde(serialize_with = "...")]`.

use super::EventError;
use crate::json::{JsonObject, JsonValue};
use serde::Serializer;
use serde::ser::Error as _;

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

/// Reject a non-finite monetary amount, carrying the reason as the error.
fn ensure_finite(value: f64) -> Result<f64, String> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(format!("monetary amount must be finite, got {value}"))
    }
}

/// Serialize a finite monetary `f64`, failing instead of silently encoding
/// a NaN or infinite value as JSON `null`.
///
/// `serde_json::to_value`'s default `f64` encoding does exactly that
/// (`serde_json::Number::from_f64` returns `None` for a non-finite value,
/// and its `Value::from(f64)` falls back to `Value::Null`), which would
/// otherwise let a cost field like `BudgetEvent::spent_usd` round-trip
/// through `None`/zero without ever surfacing an error.
pub(super) fn serialize_finite_f64<S>(value: &f64, serializer: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    let value = ensure_finite(*value).map_err(S::Error::custom)?;
    serializer.serialize_f64(value)
}

/// As [`serialize_finite_f64`], for the `Option<f64>` fields this crate
/// uses for an optional monetary amount.
pub(crate) fn serialize_finite_f64_option<S>(
    value: &Option<f64>,
    serializer: S,
) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    match value {
        Some(amount) => {
            let amount = ensure_finite(*amount).map_err(S::Error::custom)?;
            serializer.serialize_some(&amount)
        }
        None => serializer.serialize_none(),
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
