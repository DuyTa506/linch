//! The `Skill` tool: returns a skill's substituted body to the model.

use async_trait::async_trait;
use serde_json::json;

use super::{SKILL_TOOL, Skill, substitute};
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
    skills: Vec<Skill>,
    schema: JsonObject,
}

impl SkillTool {
    pub(crate) fn new(skills: Vec<Skill>) -> Self {
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
        let Some(skill) = self.skills.iter().find(|skill| skill.name == name) else {
            return Err(ToolOutputError::new(format!("Unknown skill: {name}")));
        };
        if skill.disable_model_invocation {
            let message = format!("Skill is not invokable by the model: {}", skill.name);
            return Err(ToolOutputError::new(message));
        }
        let args = input.get("args").and_then(JsonValue::as_str).unwrap_or("");
        let body = substitute(skill, args, ctx.session_id.as_str());
        Ok(ToolOutput::new(body))
    }
}
