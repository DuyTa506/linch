//! Structured output: a run's final answer as a JSON object that satisfies
//! an [`OutputSchema`].
//!
//! The answer text is parsed as Python Linch's `_parse_structured_output`
//! does: as JSON, or failing that the first fenced code block (```` ```json
//! ... ``` ````) in it. The value must be an object and must satisfy the
//! schema, compiled as draft 2020-12 like a tool's. The runner sends a
//! failure back to the model up to `structured_output_retries` times, then
//! ends the run failed (see `runner/structured.rs`).
//!
//! A run may also have a [`FinalTool`]: a tool whose call ends the run,
//! its input the answer, checked the same way (see `runner/final_tool.rs`).

use std::sync::Arc;

use async_trait::async_trait;
use jsonschema::Validator;

use crate::events::Envelope;
use crate::events::Event;
use crate::events::kinds::VerificationAction;
use crate::json::{JsonObject, JsonValue};
use crate::provider::{OutputSchema, ToolDefinition};
use crate::registry::{RegisteredTool, ToolSnapshot, compiled, truncate, violations};
use crate::tool::{Tool, ToolContext, ToolOutputError, ToolResult, ToolScope};

/// The `verifier` of the `verification` events the schema gate stages.
pub(crate) const VERIFIER: &str = "output_schema";

/// The contract `features` key of an agent that makes its own final tool
/// from its output schema; absent otherwise, so other contracts keep
/// their digests.
pub(crate) const FINAL_TOOL_FEATURE: &str = "final_tool";

/// The result of a final tool call the run took as its answer (Python's
/// acknowledgement): it pairs the call, so the session takes another run.
pub(crate) const FINAL_ACK: &str = "Final answer captured by the host.";

/// The description of a final tool made from a schema that has none.
const FINAL_DESCRIPTION: &str = "Give your final answer as this tool's input. Call it once, \
                                 as the only tool call of your response, when the task is done.";

/// Longest tool name every provider takes.
const MAX_TOOL_NAME: usize = 64;

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
        self.check_value(parse(text)?)
    }

    /// `value` as the object the schema accepts, or why it is not one,
    /// worded as [`Self::check`] words it.
    pub(crate) fn check_value(&self, value: JsonValue) -> Result<JsonObject, String> {
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
/// reminder or a final tool call's result. With a final tool, the model
/// is told to answer through it.
pub(crate) fn feedback(error: &str, final_tool: Option<&str>) -> String {
    let again = match final_tool {
        None => "Respond again with ONLY a JSON object matching the required schema.".to_owned(),
        Some(name) => format!(
            "Call the {name} tool again, as the only tool call of your response, with input \
             matching its schema."
        ),
    };
    format!("Your previous response failed structured-output validation: {error}\n{again}")
}

/// A tool whose call ends the run, its input the answer (Python's
/// `final_tool_name`).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct FinalTool {
    /// The name the model calls it by.
    pub(crate) name: String,
    /// The definition the run adds to each request: `Some` for a tool the
    /// run made from its output schema, `None` for a registered tool.
    pub(crate) made: Option<ToolDefinition>,
}

impl FinalTool {
    /// The final tool of a run with the spec's `named` one and the output
    /// schema `output`, on a provider that takes a schema `native`ly or
    /// not: the named one, else, with a schema the provider cannot take,
    /// one made from it; `None` when neither applies.
    pub(crate) fn of(
        named: Option<&str>,
        output: Option<&OutputContract>,
        native: bool,
    ) -> Option<Self> {
        if let Some(name) = named {
            return Some(Self {
                name: name.to_owned(),
                made: None,
            });
        }
        let schema = output.filter(|_| !native)?.schema();
        Some(Self::made(schema))
    }

    /// The final tool made from `schema`: named after it, its input
    /// schema the output schema.
    pub(crate) fn made(schema: &OutputSchema) -> Self {
        let description = (schema.description.clone()).unwrap_or_else(|| FINAL_DESCRIPTION.into());
        let definition = ToolDefinition {
            name: schema.name.clone(),
            description,
            input_schema: Arc::new(schema.schema.clone()),
        };
        Self {
            name: schema.name.clone(),
            made: Some(definition),
        }
    }

    /// The final tool a run recorded as `name` in its `init` event, for an
    /// agent with the spec's `named` one and the run's schema `output`:
    /// the named one, else the one made from the schema; `None` when
    /// neither is called `name` any more.
    pub(crate) fn recorded(
        name: &str,
        named: Option<&str>,
        output: Option<&OutputContract>,
    ) -> Option<Self> {
        if named == Some(name) {
            return Self::of(named, None, true);
        }
        let made = Self::made(output?.schema());
        (made.name == name).then_some(made)
    }

    /// Why a run cannot offer this tool beside the registered tools,
    /// `taken` saying whether a name is one; `None` when it can.
    pub(crate) fn problem(&self, taken: impl Fn(&str) -> bool) -> Option<String> {
        let name = &self.name;
        match &self.made {
            None if !taken(name) => {
                Some(format!("the final tool {name:?} is not a registered tool"))
            }
            None => None,
            Some(_) if taken(name) => Some(format!(
                "the output schema's name {name:?} is a registered tool's, so it cannot name \
                 the final tool made from it"
            )),
            Some(_) => tool_name_problem(name),
        }
    }

    /// The tool as the planner decides its call: the registered tool of
    /// `snapshot` for a named final tool, else a read-scope stand-in for the
    /// one made from the output schema, which is not registered.
    ///
    /// Raises:
    ///     String: Why the call cannot be planned: the named tool is no
    ///         longer registered, or the made one's schema does not compile
    ///         as a tool's.
    pub(crate) fn registered(
        &self,
        snapshot: &ToolSnapshot,
    ) -> Result<Arc<RegisteredTool>, String> {
        let name = &self.name;
        let Some(definition) = &self.made else {
            let tool = snapshot.resolve(name);
            return tool.cloned().ok_or_else(|| {
                format!("the final tool {name:?} is not a registered tool any more")
            });
        };
        let mut schema = (*definition.input_schema).clone();
        schema
            .entry("type")
            .or_insert_with(|| JsonValue::from("object"));
        let made = MadeTool {
            name: definition.name.clone(),
            description: definition.description.clone(),
            schema,
        };
        compiled(Arc::new(made)).map_err(|error| error.to_string())
    }

    /// Why a response with `calls` tool calls, one of them this tool's,
    /// cannot end the run: the final tool must be called alone.
    pub(crate) fn alone(&self, calls: usize) -> Result<(), String> {
        match calls {
            1 => Ok(()),
            _ => Err(format!(
                "the final-answer tool {} must be the only tool call in its response; none \
                 of the {calls} calls ran",
                self.name
            )),
        }
    }
}

/// The final tool made from an output schema, as a tool the planner can
/// decide a call of. It never runs: the call is the answer.
struct MadeTool {
    name: String,
    description: String,
    schema: JsonObject,
}

#[async_trait]
impl Tool for MadeTool {
    fn name(&self) -> &str {
        &self.name
    }

    fn description(&self) -> &str {
        &self.description
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    /// Giving an answer touches nothing.
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Err(ToolOutputError::new("the final tool is not run"))
    }
}

/// Why `name` is not a tool name every provider takes (OpenAI's and
/// Anthropic's rule, 1 to 64 of `A-Z a-z 0-9 _ -`); `None` when it is.
pub(crate) fn tool_name_problem(name: &str) -> Option<String> {
    let legal = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    let fits = !name.is_empty() && name.len() <= MAX_TOOL_NAME && name.chars().all(legal);
    (!fits).then(|| {
        format!("{name:?} cannot name a tool: use 1 to {MAX_TOOL_NAME} letters, digits, _ or -")
    })
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

    #[test]
    fn the_final_tool_is_the_named_one_else_one_made_without_native_output() {
        let invoice = invoice();
        assert_eq!(FinalTool::of(None, Some(&invoice), true), None);
        let made = FinalTool::of(None, Some(&invoice), false).unwrap();
        let definition = made.made.as_ref().unwrap();
        assert_eq!(
            (made.name.as_str(), definition.name.as_str()),
            ("invoice", "invoice")
        );
        assert_eq!(*definition.input_schema, invoice.schema().schema);
        assert_eq!(definition.description, FINAL_DESCRIPTION);
        let named = FinalTool::of(Some("Submit"), Some(&invoice), false).unwrap();
        assert_eq!((named.name.as_str(), named.made.as_ref()), ("Submit", None));
        // A resume finds the one the run recorded.
        assert_eq!(
            FinalTool::recorded("Submit", Some("Submit"), None),
            Some(named)
        );
        assert_eq!(
            FinalTool::recorded("invoice", None, Some(&invoice)),
            Some(made)
        );
        assert_eq!(FinalTool::recorded("other", None, Some(&invoice)), None);
    }

    #[test]
    fn a_made_final_tool_needs_a_free_legal_name() {
        let made = FinalTool::made(invoice().schema());
        assert_eq!(made.problem(|_| false), None);
        assert!(made.problem(|name| name == "invoice").is_some());
        let named = FinalTool::of(Some("Submit"), None, true).unwrap();
        assert!(
            named
                .problem(|_| false)
                .unwrap()
                .contains("not a registered")
        );
        assert_eq!(named.problem(|_| true), None);
        for bad in ["", "my tool", "naïve", &"x".repeat(65)] {
            assert!(tool_name_problem(bad).is_some(), "{bad:?}");
        }
        assert_eq!(tool_name_problem(&"x".repeat(64)), None);
        assert!(made.alone(1).is_ok());
        assert!(made.alone(2).unwrap_err().contains("only tool call"));
        let tool = feedback("bad", Some("invoice"));
        assert!(tool.ends_with(
            "Call the invoice tool again, as the only tool call of your \
                                response, with input matching its schema."
        ));
        assert!(feedback("bad", None).ends_with("matching the required schema."));
    }
}
