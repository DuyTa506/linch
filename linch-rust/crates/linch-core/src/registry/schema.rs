//! Compiled tool schemas: input validation and the output contract.

use std::fmt;
use std::sync::Arc;

use jsonschema::{ValidationError, Validator};
use thiserror::Error;

use super::RegistryError;
use crate::json::{JsonObject, JsonValue};
use crate::provider::ToolDefinition;
use crate::tool::{Tool, ToolOutput, ToolOutputError};

/// Error code on a [`ToolOutputError`] for input that fails the input schema.
pub const INVALID_INPUT_CODE: &str = "invalid_input";

/// Most schema violations listed in one model-facing error.
const MAX_LISTED_VIOLATIONS: usize = 5;
/// Longest `path: message` line kept for one violation, in characters.
const MAX_LINE_CHARS: usize = 240;

/// Which of a tool's schemas an error refers to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SchemaKind {
    /// The `input_schema` the model's arguments must satisfy.
    Input,
    /// The `output_schema` a successful result must satisfy.
    Output,
}

impl fmt::Display for SchemaKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Input => "input",
            Self::Output => "output",
        })
    }
}

/// A successful tool output that violates the tool's `output_schema`.
///
/// This is a bug in the tool, not something the model can correct, so it is
/// kept apart from [`ToolOutputError`].
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("tool {tool:?} output violates its output_schema at {path}: {message}")]
pub struct OutputContractError {
    /// Name of the tool that produced the output.
    pub tool: String,
    /// JSON pointer into the output value, or `(root)`.
    pub path: String,
    /// What the schema rejected, truncated to a bounded length.
    pub message: String,
}

/// A registered tool with its schemas compiled once, at registration.
///
/// The [`ToolDefinition`] offered to the model is captured at the same time,
/// so the schema the model sees is the schema its input is checked against.
pub struct RegisteredTool {
    tool: Arc<dyn Tool>,
    definition: ToolDefinition,
    input: Validator,
    output: Option<Validator>,
}

impl RegisteredTool {
    /// Compile `tool`'s schemas. The caller has already validated the name.
    pub(super) fn compile(tool: Arc<dyn Tool>) -> Result<Self, RegistryError> {
        let name = tool.name().to_owned();
        let input_schema = tool.input_schema().clone();
        if input_schema.get("type").and_then(JsonValue::as_str) != Some("object") {
            return Err(RegistryError::InputNotObject { name });
        }
        let input = compile(&name, SchemaKind::Input, &input_schema)?;
        let output = tool
            .output_schema()
            .map(|schema| compile(&name, SchemaKind::Output, schema))
            .transpose()?;
        let definition = ToolDefinition {
            name,
            description: tool.description().to_owned(),
            input_schema,
        };
        Ok(Self {
            tool,
            definition,
            input,
            output,
        })
    }

    /// The tool itself, for execution.
    pub fn tool(&self) -> &Arc<dyn Tool> {
        &self.tool
    }

    /// Name the tool was registered under.
    pub fn name(&self) -> &str {
        &self.definition.name
    }

    /// Schema offered to the model, as captured at registration.
    pub fn definition(&self) -> &ToolDefinition {
        &self.definition
    }

    /// Check model-supplied input, then run [`Tool::validate`] on it.
    ///
    /// Schema violations come back as a [`ToolOutputError`] with code
    /// [`INVALID_INPUT_CODE`] and a bounded message listing each violation's
    /// JSON pointer (`(root)` for the top level) and reason, so the model can
    /// fix its call. `Tool::validate` only runs on schema-valid input; its
    /// result (the input to execute with, or its error) is returned as-is.
    pub fn validate_input(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        let value = JsonValue::Object(input);
        if !self.input.is_valid(&value) {
            return Err(self.invalid_input(&value));
        }
        let JsonValue::Object(input) = value else {
            unreachable!("value was built from an object")
        };
        self.tool.validate(input)
    }

    /// Check a successful output value against the tool's `output_schema`.
    ///
    /// A tool without an output schema accepts every value.
    pub fn validate_output(&self, output: &ToolOutput) -> Result<(), OutputContractError> {
        let Some(validator) = &self.output else {
            return Ok(());
        };
        validator
            .validate(&output.value)
            .map_err(|error| OutputContractError {
                tool: self.name().to_owned(),
                path: location(&error),
                message: truncate(&error.to_string(), MAX_LINE_CHARS),
            })
    }

    fn invalid_input(&self, value: &JsonValue) -> ToolOutputError {
        let mut message = format!(
            "Input for tool {:?} does not match its schema:",
            self.name()
        );
        let mut total = 0;
        for error in self.input.iter_errors(value) {
            total += 1;
            if total <= MAX_LISTED_VIOLATIONS {
                let line = format!("{}: {error}", location(&error));
                message.push_str("\n- ");
                message.push_str(&truncate(&line, MAX_LINE_CHARS));
            }
        }
        if total > MAX_LISTED_VIOLATIONS {
            let hidden = total - MAX_LISTED_VIOLATIONS;
            message.push_str(&format!("\n({hidden} more not shown)"));
        }
        ToolOutputError {
            message,
            code: Some(INVALID_INPUT_CODE.to_owned()),
            details: None,
        }
    }
}

impl fmt::Debug for RegisteredTool {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RegisteredTool")
            .field("name", &self.name())
            .field("scope", &self.tool.scope())
            .field("has_output_schema", &self.output.is_some())
            .finish()
    }
}

/// Compile `schema` as draft 2020-12 regardless of its `$schema`, matching
/// Python's `Draft202012Validator`. Remote and file `$ref`s fail here: the
/// crate is built without retrievers, so registration never does I/O.
fn compile(name: &str, kind: SchemaKind, schema: &JsonObject) -> Result<Validator, RegistryError> {
    jsonschema::draft202012::new(&JsonValue::Object(schema.clone())).map_err(|error| {
        RegistryError::InvalidSchema {
            name: name.to_owned(),
            kind,
            message: format!("{}: {error}", location(&error)),
        }
    })
}

fn location(error: &ValidationError<'_>) -> String {
    match error.instance_path().as_str() {
        "" => "(root)".to_owned(),
        path => path.to_owned(),
    }
}

/// `text` cut to `max` characters, with `...` marking a cut.
fn truncate(text: &str, max: usize) -> String {
    match text.char_indices().nth(max) {
        Some((end, _)) => format!("{}...", &text[..end]),
        None => text.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::registry::tests::{object, schema_tool};

    fn compiled(
        input: JsonValue,
        output: Option<JsonValue>,
    ) -> Result<RegisteredTool, RegistryError> {
        RegisteredTool::compile(schema_tool("counter", input, output))
    }

    fn counter() -> RegisteredTool {
        let input = json!({
            "type": "object",
            "properties": {"count": {"type": "integer", "maximum": 100}},
            "required": ["count"],
        });
        let output = json!({"type": "object", "properties": {"count": {"type": "integer"}}});
        compiled(input, Some(output)).unwrap()
    }

    #[test]
    fn input_schema_must_be_a_valid_object_schema() {
        for not_object in [
            json!({}),
            json!({"type": "array"}),
            json!({"type": ["object"]}),
        ] {
            assert_eq!(
                compiled(not_object, None).unwrap_err(),
                RegistryError::InputNotObject {
                    name: "counter".into()
                }
            );
        }
        let bad = compiled(
            json!({"type": "object", "properties": {"n": {"type": "nope"}}}),
            None,
        );
        assert!(matches!(
            bad.unwrap_err(),
            RegistryError::InvalidSchema { kind: SchemaKind::Input, ref message, .. }
                if message.starts_with("/properties/n/type: ")
        ));
        // A remote `$ref` is rejected at registration instead of fetched.
        let remote = compiled(
            json!({"type": "object", "$ref": "https://example.com/s.json"}),
            None,
        );
        assert!(matches!(
            remote.unwrap_err(),
            RegistryError::InvalidSchema {
                kind: SchemaKind::Input,
                ..
            }
        ));
        let output = compiled(json!({"type": "object"}), Some(json!({"minLength": -1})));
        assert!(matches!(
            output.unwrap_err(),
            RegistryError::InvalidSchema {
                kind: SchemaKind::Output,
                ..
            }
        ));
    }

    #[test]
    fn schema_errors_name_the_field_for_the_model() {
        let counter = counter();
        let error = counter
            .validate_input(object(json!({"count": "three"})))
            .unwrap_err();
        assert_eq!(error.code.as_deref(), Some(INVALID_INPUT_CODE));
        assert!(error.message.contains("\"counter\""), "{}", error.message);
        assert!(error.message.contains("/count: "), "{}", error.message);

        let missing = counter.validate_input(JsonObject::new()).unwrap_err();
        assert!(missing.message.contains("(root): "), "{}", missing.message);
    }

    #[test]
    fn schema_errors_are_bounded() {
        let mut properties = JsonObject::new();
        let mut input = JsonObject::new();
        for i in 0..50 {
            properties.insert(format!("f{i}"), json!({"type": "integer"}));
            input.insert(format!("f{i}"), json!("x".repeat(10_000)));
        }
        let wide = compiled(json!({"type": "object", "properties": properties}), None).unwrap();
        let error = wide.validate_input(input).unwrap_err();
        assert_eq!(error.message.matches("\n- ").count(), MAX_LISTED_VIOLATIONS);
        assert!(error.message.ends_with("(45 more not shown)"));
        assert!(error.message.chars().count() < 1_500, "{}", error.message);
    }

    #[test]
    fn tool_validate_runs_only_after_the_schema_passes() {
        let counter = counter();
        let ok = counter.validate_input(object(json!({"count": 3}))).unwrap();
        assert_eq!(ok.get("mode"), Some(&json!("fast")));
        let rejected = counter
            .validate_input(object(json!({"count": 11})))
            .unwrap_err();
        assert_eq!(rejected.message, "count must be at most 10");
        assert_eq!(rejected.code, None);
        // Both checks reject 200; the schema runs first, so it reports.
        let both = counter
            .validate_input(object(json!({"count": 200})))
            .unwrap_err();
        assert_eq!(both.code.as_deref(), Some(INVALID_INPUT_CODE));
    }

    #[test]
    fn schemas_compile_as_draft_2020_12_without_format_assertions() {
        let schema = json!({
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "pair": {"prefixItems": [{"type": "integer"}]},
                "mail": {"format": "email"},
            },
        });
        let old = compiled(schema, None).unwrap();
        // `prefixItems` only exists in 2020-12; draft-07 would ignore it.
        let error = old
            .validate_input(object(json!({"pair": ["x"]})))
            .unwrap_err();
        assert!(error.message.contains("/pair/0: "), "{}", error.message);
        // Python's Draft202012Validator treats `format` as an annotation only.
        assert!(old.validate_input(object(json!({"mail": "nope"}))).is_ok());
    }

    #[test]
    fn output_contract_is_checked_on_success_values() {
        let counter = counter();
        let valid = ToolOutput::new(json!({"count": 2}));
        assert!(counter.validate_output(&valid).is_ok());
        let error = counter
            .validate_output(&ToolOutput::new(json!({"count": "two"})))
            .unwrap_err();
        assert_eq!(error.tool, "counter");
        assert_eq!(error.path, "/count");

        let plain = compiled(json!({"type": "object"}), None).unwrap();
        assert!(plain.validate_output(&ToolOutput::new(json!(42))).is_ok());
    }

    #[test]
    fn truncate_cuts_on_char_boundaries() {
        assert_eq!(truncate("héllo", 2), "hé...");
        assert_eq!(truncate("héllo", 5), "héllo");
    }
}
