//! Input fields and error mapping shared by the tools.
//!
//! The registry validates input against each tool's schema before
//! `execute`, so these checks only guard a host that calls a tool directly.

use linch_core::registry::INVALID_INPUT_CODE;
use linch_core::{JsonObject, JsonValue, ToolOutputError};

use crate::fs::FsError;
use crate::tracker::ReadFirst;

/// Error code of a refused write that needs a `Read` first.
pub const READ_FIRST_CODE: &str = "read_before_write";

/// A schema literal as an object.
pub(crate) fn schema(value: JsonValue) -> JsonObject {
    match value {
        JsonValue::Object(object) => object,
        _ => unreachable!("tool schemas are object literals"),
    }
}

/// A required string field.
pub(crate) fn string<'a>(input: &'a JsonObject, key: &str) -> Result<&'a str, ToolOutputError> {
    optional_string(input, key)?.ok_or_else(|| invalid(format!("{key} is required")))
}

/// An optional string field.
pub(crate) fn optional_string<'a>(
    input: &'a JsonObject,
    key: &str,
) -> Result<Option<&'a str>, ToolOutputError> {
    match input.get(key) {
        None | Some(JsonValue::Null) => Ok(None),
        Some(JsonValue::String(text)) => Ok(Some(text)),
        Some(_) => Err(invalid(format!("{key} must be a string"))),
    }
}

/// An optional non-negative integer field.
pub(crate) fn optional_count(
    input: &JsonObject,
    key: &str,
) -> Result<Option<u64>, ToolOutputError> {
    match input.get(key) {
        None | Some(JsonValue::Null) => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| invalid(format!("{key} must be a non-negative integer"))),
    }
}

/// An optional boolean field, `false` when absent.
pub(crate) fn flag(input: &JsonObject, key: &str) -> Result<bool, ToolOutputError> {
    match input.get(key) {
        None | Some(JsonValue::Null) => Ok(false),
        Some(JsonValue::Bool(value)) => Ok(*value),
        Some(_) => Err(invalid(format!("{key} must be a boolean"))),
    }
}

/// An invalid-input error.
pub(crate) fn invalid(message: impl Into<String>) -> ToolOutputError {
    ToolOutputError {
        message: message.into(),
        code: Some(INVALID_INPUT_CODE.to_owned()),
        details: None,
    }
}

/// An error with a machine-readable code.
pub(crate) fn coded(message: impl Into<String>, code: &str) -> ToolOutputError {
    ToolOutputError {
        message: message.into(),
        code: Some(code.to_owned()),
        details: None,
    }
}

impl From<FsError> for ToolOutputError {
    fn from(error: FsError) -> Self {
        let code = match &error {
            FsError::Escape { .. } => "path_escape",
            FsError::NotFound { .. } => "not_found",
            FsError::NotAFile { .. } => "not_a_file",
            FsError::NotADirectory { .. } => "not_a_directory",
            FsError::TooLarge { .. } => "too_large",
            FsError::Changed { .. } => "changed",
            FsError::InvalidQuery(_) => "invalid_query",
            FsError::Cancelled => "cancelled",
            FsError::Io { .. } => "io",
        };
        coded(error.to_string(), code)
    }
}

impl From<ReadFirst> for ToolOutputError {
    fn from(error: ReadFirst) -> Self {
        coded(error.to_string(), READ_FIRST_CODE)
    }
}
