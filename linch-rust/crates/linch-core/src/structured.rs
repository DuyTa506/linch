//! Structured output: a run's final answer as a JSON object that satisfies
//! an [`OutputSchema`].
//!
//! The answer text is parsed as Python Linch's `_parse_structured_output`
//! does: as JSON, or failing that the first fenced code block (```` ```json
//! ... ``` ````) in it. The value must be an object and must satisfy the
//! schema, compiled as draft 2020-12 like a tool's. The runner sends a
//! failure back to the model up to `structured_output_retries` times, then
//! ends the run failed (see `runner/structured.rs`).

use jsonschema::Validator;

use crate::events::Envelope;
use crate::events::Event;
use crate::events::kinds::VerificationAction;
use crate::json::{JsonObject, JsonValue};
use crate::provider::OutputSchema;
use crate::registry::{truncate, violations};

/// The `verifier` of the `verification` events the schema gate stages.
pub(crate) const VERIFIER: &str = "output_schema";

/// Longest JSON parse error kept, in characters.
const MAX_PARSE_CHARS: usize = 240;

/// An [`OutputSchema`] compiled once, for the answers of many runs.
pub(crate) struct OutputContract {
    schema: OutputSchema,
    validator: Validator,
}

impl OutputContract {
    /// Compile `schema`.
    ///
    /// Raises:
    ///     String: Why the schema cannot be used: a blank name, a root
    ///         `type` other than `object`, or a schema that does not
    ///         compile.
    pub(crate) fn compile(schema: OutputSchema) -> Result<Self, String> {
        if schema.name.trim().is_empty() {
            return Err("the output schema's name must not be blank".into());
        }
        if let Some(kind) = schema.schema.get("type")
            && kind != "object"
        {
            return Err(format!(
                "the output schema's root type must be \"object\", not {kind}"
            ));
        }
        let value = JsonValue::Object(schema.schema.clone());
        let validator = jsonschema::draft202012::new(&value)
            .map_err(|error| format!("the output schema does not compile: {error}"))?;
        Ok(Self { schema, validator })
    }

    /// The schema as given, for the provider request.
    pub(crate) fn schema(&self) -> &OutputSchema {
        &self.schema
    }

    /// The object `text` holds, or why it is not one the schema accepts,
    /// worded for the model to fix.
    pub(crate) fn check(&self, text: &str) -> Result<JsonObject, String> {
        let value = parse(text)?;
        if !value.is_object() {
            return Err(format!("Expected a JSON object, got {}", kind(&value)));
        }
        if !self.validator.is_valid(&value) {
            let found = violations(&self.validator, &value);
            return Err(format!("Schema validation error:{found}"));
        }
        let JsonValue::Object(object) = value else {
            unreachable!("checked to be an object above")
        };
        Ok(object)
    }
}

/// The feedback a failed answer sends back to the model (Python's
/// `_evaluate_terminal_gates` wording), before it is put in a system
/// reminder.
pub(crate) fn feedback(error: &str) -> String {
    format!(
        "Your previous response failed structured-output validation: {error}\n\
         Respond again with ONLY a JSON object matching the required schema."
    )
}

/// How many schema retries `events`, a run's durable log, already spent:
/// one per `verification` retry of the schema gate. A resumed run counts
/// them again, so the bound holds across crashes.
pub(crate) fn retries_spent(events: &[Envelope]) -> u32 {
    let spent = events.iter().filter(|envelope| {
        matches!(&envelope.event, Event::Verification(event)
            if event.verifier == VERIFIER && event.action == VerificationAction::Retry)
    });
    u32::try_from(spent.count()).unwrap_or(u32::MAX)
}

/// `text` as JSON, else the contents of its first fenced code block as
/// JSON; the first parse error when neither is.
fn parse(text: &str) -> Result<JsonValue, String> {
    let error = match serde_json::from_str(text.trim()) {
        Ok(value) => return Ok(value),
        Err(error) => error,
    };
    if let Some(Ok(value)) = fenced(text).map(|inner| serde_json::from_str(inner.trim())) {
        return Ok(value);
    }
    let message = truncate(&error.to_string(), MAX_PARSE_CHARS);
    Err(format!("JSON parse error: {message}"))
}

/// The body of the first ```` ``` ```` fence of `text`, without a `json`
/// tag (any case).
fn fenced(text: &str) -> Option<&str> {
    let start = text.find("```")? + 3;
    let rest = &text[start..];
    let rest = match rest.get(..4) {
        Some(tag) if tag.eq_ignore_ascii_case("json") => &rest[4..],
        _ => rest,
    };
    let end = rest.find("```")?;
    Some(&rest[..end])
}

fn kind(value: &JsonValue) -> &'static str {
    match value {
        JsonValue::Null => "null",
        JsonValue::Bool(_) => "boolean",
        JsonValue::Number(_) => "number",
        JsonValue::String(_) => "string",
        JsonValue::Array(_) => "array",
        JsonValue::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn contract(schema: JsonValue) -> Result<OutputContract, String> {
        let JsonValue::Object(schema) = schema else {
            panic!("an object schema")
        };
        OutputContract::compile(OutputSchema {
            name: "invoice".into(),
            schema,
            strict: true,
            description: None,
        })
    }

    fn invoice() -> OutputContract {
        contract(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
            "additionalProperties": false,
        }))
        .unwrap()
    }

    #[test]
    fn a_json_object_that_fits_is_the_value() {
        let value = invoice().check(" {\"total\": 4.5}\n").unwrap();
        assert_eq!(JsonValue::Object(value), json!({"total": 4.5}));
    }

    #[test]
    fn a_fenced_block_is_read_when_the_text_is_not_json() {
        let text = "Here it is:\n```JSON\n{\"total\": 1}\n```\nDone.";
        assert_eq!(invoice().check(text).unwrap()["total"], json!(1));
        let bare = "```\n{\"total\": 2}\n```";
        assert_eq!(invoice().check(bare).unwrap()["total"], json!(2));
    }

    #[test]
    fn each_failure_says_what_to_fix() {
        let parse = invoice().check("total is 4").unwrap_err();
        assert!(parse.starts_with("JSON parse error: "), "{parse}");
        let array = invoice().check("[1]").unwrap_err();
        assert_eq!(array, "Expected a JSON object, got array");
        let missing = invoice().check("{\"sum\": 1}").unwrap_err();
        assert!(
            missing.starts_with("Schema validation error:\n- "),
            "{missing}"
        );
        assert!(missing.contains("total"), "{missing}");
    }

    #[test]
    fn a_schema_must_compile_to_an_object_with_a_name() {
        assert!(contract(json!({"type": "array"})).is_err());
        assert!(contract(json!({"type": "object", "minProperties": "x"})).is_err());
        assert!(
            contract(json!({"properties": {}})).is_ok(),
            "type is optional"
        );
        let blank = OutputContract::compile(OutputSchema {
            name: " ".into(),
            schema: JsonObject::new(),
            strict: true,
            description: None,
        });
        assert!(blank.is_err());
    }
}
