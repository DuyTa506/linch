//! The built-in [`Scorer`]s.

use linch_core::events::kinds::{ContextBuildEvent, ResultSubtype};
use linch_core::{Envelope, Event, JsonObject, JsonValue};
use serde::{Deserialize, Serialize};

use super::harness::{EvalCase, result_event};

#[cfg(test)]
mod tests;

/// A built-in check on one case's run.
///
/// Its serde form is the suite file's scorer entry: the variant name in
/// snake case under `type`, plus the variant's fields, for example
/// `{"type": "text_contains", "substring": "{expected}"}`. Unknown types and
/// unknown fields are rejected.
///
/// The `context_*` scorers read `context_build` events, which the Rust
/// runner does not emit yet: on its runs they return `None`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Scorer {
    /// Passes when the output contains `substring`, ignoring case. Each
    /// `{expected}` in it is replaced by the case's expected answer first.
    TextContains {
        /// Text to look for; `{expected}` when absent.
        #[serde(default = "expected_placeholder")]
        substring: String,
    },
    /// Passes when the run started a call of `tool`.
    ToolCalled {
        /// Tool name.
        tool: String,
    },
    /// Passes when the output is JSON that `schema` accepts. An output that
    /// is not JSON, or a schema that is not valid, fails, so an unchecked
    /// output never passes.
    SchemaValid {
        /// JSON Schema; draft 2020-12 unless it names another in `$schema`.
        schema: JsonObject,
    },
    /// Passes when a `context_build` event offered `tool`; unknown when the
    /// run has none.
    ContextSelectedTool {
        /// Tool name.
        tool: String,
    },
    /// Passes when no `context_build` event reports `budget.trimmed: true`;
    /// unknown when the run has none.
    ContextNotTrimmed {},
    /// Passes when a `context_build` event's metadata holds `key`, with the
    /// value `expected` when that is set; unknown when the run has none.
    ContextMetadataContains {
        /// Metadata key.
        key: String,
        /// Required value; any value when absent or `null`.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        expected: Option<JsonValue>,
    },
    /// Passes when a failed tool call is followed by a successful one, of
    /// `tool` when set. Fails when tool calls ended but none recovered;
    /// unknown when none ended.
    RecoverySucceeded {
        /// Tool name; every tool when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        tool: Option<String>,
    },
    /// Passes when the run ended with a successful `result`; unknown when
    /// it ended without one (a suspended or broken run).
    RunCompleted {},
}

fn expected_placeholder() -> String {
    "{expected}".to_owned()
}

impl Scorer {
    /// The scorer's name in reports, as Python names it: the type, then
    /// `:` and its argument when it has one.
    pub fn name(&self) -> String {
        match self {
            Self::TextContains { substring } => format!("text_contains:{substring}"),
            Self::ToolCalled { tool } => format!("tool_called:{tool}"),
            Self::SchemaValid { .. } => "schema_valid".to_owned(),
            Self::ContextSelectedTool { tool } => format!("context_selected_tool:{tool}"),
            Self::ContextNotTrimmed {} => "context_not_trimmed".to_owned(),
            Self::ContextMetadataContains { key, .. } => {
                format!("context_metadata_contains:{key}")
            }
            Self::RecoverySucceeded { tool } => {
                format!("recovery_succeeded:{}", tool.as_deref().unwrap_or("*"))
            }
            Self::RunCompleted {} => "run_completed".to_owned(),
        }
    }

    /// Score one case's run.
    ///
    /// Args:
    ///     case: The case that ran, for its expected answer.
    ///     output: The run's final text.
    ///     events: Every envelope the run streamed.
    ///
    /// Returns:
    ///     `Some(true)` or `Some(false)`, or `None` when the run lacks the
    ///     data this scorer reads.
    pub fn score(&self, case: &EvalCase, output: &str, events: &[Envelope]) -> Option<bool> {
        match self {
            Self::TextContains { substring } => {
                let wanted = substring.replace("{expected}", &case.expected);
                Some(output.to_lowercase().contains(&wanted.to_lowercase()))
            }
            Self::ToolCalled { tool } => Some(events.iter().any(|envelope| {
                matches!(&envelope.event, Event::ToolCallStart(start) if start.tool_name == *tool)
            })),
            Self::SchemaValid { schema } => Some(schema_accepts(schema, output)),
            Self::ContextSelectedTool { tool } => context(events, |builds| {
                builds.iter().any(|build| {
                    build
                        .selected_tools
                        .as_ref()
                        .is_some_and(|tools| tools.contains(tool))
                })
            }),
            Self::ContextNotTrimmed {} => context(events, |builds| {
                let trimmed = Some(&JsonValue::Bool(true));
                builds.iter().all(|build| build.budget.get("trimmed") != trimmed)
            }),
            Self::ContextMetadataContains { key, expected } => context(events, |builds| {
                builds.iter().any(|build| {
                    build
                        .metadata
                        .get(key)
                        .is_some_and(|value| {
                            expected.as_ref().is_none_or(|e| same_value(e, value))
                        })
                })
            }),
            Self::RecoverySucceeded { tool } => recovered(events, tool.as_deref()),
            Self::RunCompleted {} => {
                result_event(events).map(|result| result.subtype == ResultSubtype::Success)
            }
        }
    }
}

fn schema_accepts(schema: &JsonObject, output: &str) -> bool {
    let Ok(instance) = serde_json::from_str::<JsonValue>(output) else {
        return false;
    };
    let schema = JsonValue::Object(schema.clone());
    jsonschema::validator_for(&schema).is_ok_and(|validator| validator.is_valid(&instance))
}

/// Whether `a` equals `b` as Python's `==` compares decoded JSON: numbers
/// by value (`1 == 1.0`), arrays and objects member by member.
fn same_value(a: &JsonValue, b: &JsonValue) -> bool {
    match (a, b) {
        (JsonValue::Number(a), JsonValue::Number(b)) => same_number(a, b),
        (JsonValue::Array(a), JsonValue::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| same_value(a, b))
        }
        (JsonValue::Object(a), JsonValue::Object(b)) => {
            a.len() == b.len()
                && (a.iter()).all(|(key, a)| b.get(key).is_some_and(|b| same_value(a, b)))
        }
        _ => a == b,
    }
}

/// Exact numeric equality across integers and floats, like Python's: an
/// integer equals a float only when the float is that exact integer.
fn same_number(a: &serde_json::Number, b: &serde_json::Number) -> bool {
    match (integer(a), integer(b)) {
        (Some(a), Some(b)) => a == b,
        (None, None) => a.as_f64() == b.as_f64(),
        _ => false,
    }
}

/// The number as an integer, when it is one (an integral float included).
fn integer(number: &serde_json::Number) -> Option<i128> {
    if let Some(value) = number.as_i64() {
        return Some(value.into());
    }
    if let Some(value) = number.as_u64() {
        return Some(value.into());
    }
    let value = number.as_f64()?;
    // Integral floats of |value| < 2^127 convert exactly.
    (value.fract() == 0.0 && value.abs() < 2f64.powi(127)).then_some(value as i128)
}

/// `verdict` over the run's `context_build` events; `None` without one.
fn context(
    events: &[Envelope],
    verdict: impl FnOnce(&[&ContextBuildEvent]) -> bool,
) -> Option<bool> {
    let builds: Vec<&ContextBuildEvent> = events
        .iter()
        .filter_map(|envelope| match &envelope.event {
            Event::ContextBuild(build) => Some(build),
            _ => None,
        })
        .collect();
    (!builds.is_empty()).then(|| verdict(&builds))
}

/// Whether a failed call of `tool` (any tool when `None`) was followed by a
/// successful one; `None` when no such call ended.
fn recovered(events: &[Envelope], tool: Option<&str>) -> Option<bool> {
    let (mut ended, mut failed) = (false, false);
    for envelope in events {
        let Event::ToolCallEnd(end) = &envelope.event else {
            continue;
        };
        if tool.is_some_and(|tool| tool != end.tool_name) {
            continue;
        }
        ended = true;
        if end.is_error() {
            failed = true;
        } else if failed {
            return Some(true);
        }
    }
    ended.then_some(false)
}
