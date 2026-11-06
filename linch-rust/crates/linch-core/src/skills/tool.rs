//! The `Skill` tool: returns a skill's substituted body to the model.

use async_trait::async_trait;
use serde_json::json;

use std::sync::Arc;

use super::{SKILL_TOOL, SkillSet, substitute};
use crate::agent::Versions;
use crate::events::Event;
use crate::events::kinds::{SkillCompletedEvent, SkillInvokedEvent};
use crate::json::{JsonObject, JsonValue};
use crate::tool::{Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

const DESCRIPTION: &str = "Invoke a skill by name. Skills are listed in the system prompt. \
The skill's body is returned to you so you can act on its instructions in the next turn.";

/// Loads one of the agent's skills by name.
///
/// Read scope: it only returns text, so it runs without approval and
/// alongside other read calls; a rule with a `skill` condition can still
/// deny or ask for one skill.
pub(crate) struct SkillTool {
    /// The agent's skills; a reload changes the current set.
    skills: Arc<Versions<SkillSet>>,
    schema: JsonObject,
}

impl SkillTool {
    pub(crate) fn new(skills: Arc<Versions<SkillSet>>) -> Self {
        let schema = json!({
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Name of the skill to invoke."},
                "args": {
                    "type": "string",
                    "description": "Optional argument string forwarded to the skill body.",
                },
            },
            "required": ["skill"],
        });
        let JsonValue::Object(schema) = schema else {
            unreachable!("the schema is an object")
        };
        Self { skills, schema }
    }
}

fn requested(input: &JsonObject) -> &str {
    let name = input.get("skill").and_then(JsonValue::as_str).unwrap_or("");
    name.strip_prefix('/').unwrap_or(name)
}

#[async_trait]
impl Tool for SkillTool {
    fn name(&self) -> &str {
        SKILL_TOOL
    }

    fn description(&self) -> &str {
        DESCRIPTION
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if requested(&input).trim().is_empty() {
            return Err(ToolOutputError::new("skill must be a non-empty string"));
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Invoke skill: {}", requested(input))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let name = requested(&input);
        let args = input.get("args").and_then(JsonValue::as_str);
        ctx.emit(Event::SkillInvoked(SkillInvokedEvent {
            name: name.to_owned(),
            args: args.map(str::to_owned),
            model_override: None,
            allowed_tools: None,
        }));
        let result = self.load(name, args.unwrap_or(""), &ctx);
        ctx.emit(Event::SkillCompleted(SkillCompletedEvent {
            name: name.to_owned(),
            is_error: result.is_err(),
        }));
        result
    }
}

impl SkillTool {
    fn load(&self, name: &str, args: &str, ctx: &ToolContext) -> ToolResult {
        let set = self.skills.current();
        let Some(skill) = set.skills.iter().find(|skill| skill.name == name) else {
            return Err(ToolOutputError::new(format!("Unknown skill: {name}")));
        };
        if skill.disable_model_invocation {
            let message = format!("Skill is not invokable by the model: {}", skill.name);
            return Err(ToolOutputError::new(message));
        }
        let body = substitute(skill, args, ctx.session_id.as_str());
        Ok(ToolOutput::new(body))
    }
}
