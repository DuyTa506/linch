//! The `Subagent` tool: one foreground child run per call.

use std::sync::{Arc, OnceLock, Weak};

use async_trait::async_trait;
use serde_json::json;

use super::{Roster, SUBAGENT_TOOL, run};
use crate::agent::AgentInner;
use crate::json::{JsonObject, JsonValue};
use crate::tool::{Tool, ToolContext, ToolOutputError, ToolResult, ToolScope};

const HEADER: &str = "Launch a subagent to handle a focused task.

Delegation rules:
- Fresh subagents start with no parent history; include complete context,
  relevant files, constraints, prior findings, and the exact expected output.
- Use subagents for meaningful research, implementation, or verification work.
- Do not delegate synthesis with vague prompts like \"based on your findings\";
  synthesize yourself, then assign specific next actions.
- Launch independent research tasks in parallel when they do not depend
  on each other.";

const TRAILER: &str =
    "Omit `subagent_type` to spawn a general-purpose subagent with full tool access.
The subagent returns a single text response; its tool calls and partial output
stream into your event log while it runs.";

/// Runs a subagent as a child run of the calling run.
pub(crate) struct SubagentTool {
    roster: Arc<Roster>,
    /// The agent this tool belongs to, set once it is built. Weak, since
    /// the agent holds the registry that holds this tool.
    agent: Arc<OnceLock<Weak<AgentInner>>>,
    description: String,
    schema: JsonObject,
}

impl SubagentTool {
    pub(crate) fn new(roster: Arc<Roster>, agent: Arc<OnceLock<Weak<AgentInner>>>) -> Self {
        let mut description = HEADER.to_owned();
        if !roster.listed().is_empty() {
            description.push_str("\n\nAvailable subagent types:");
            for definition in roster.listed() {
                let line = format!("\n- {}: {}", definition.name, definition.description);
                description.push_str(&line);
            }
        }
        description.push_str("\n\n");
        description.push_str(TRAILER);
        let schema = json!({
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word label for the spawned subagent (shown in UI).",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the subagent. Provide complete context \
                                    \u{2014} the subagent has no parent history.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "Optional: pick from the listed agent types. Omit to spawn \
                                    a general-purpose subagent with full tool access.",
                },
            },
            "required": ["description", "prompt"],
        });
        let JsonValue::Object(schema) = schema else {
            unreachable!("the schema is an object")
        };
        Self {
            roster,
            agent,
            description,
            schema,
        }
    }

    fn agent(&self) -> Option<Arc<AgentInner>> {
        self.agent.get().and_then(Weak::upgrade)
    }
}

fn text<'a>(input: &'a JsonObject, field: &str) -> Option<&'a str> {
    input.get(field).and_then(JsonValue::as_str)
}

#[async_trait]
impl Tool for SubagentTool {
    fn name(&self) -> &str {
        SUBAGENT_TOOL
    }

    fn description(&self) -> &str {
        &self.description
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    /// Only a named type whose definition lists its tools, each of them a
    /// registered read-scope tool; anything unknown fails closed.
    fn parallel(&self, input: &JsonObject) -> bool {
        let Some(name) = text(input, "subagent_type") else {
            return false;
        };
        let Some(definition) = self.roster.named(name.trim()) else {
            return false;
        };
        let (Some(tools), Some(agent)) = (&definition.tools, self.agent()) else {
            return false;
        };
        let snapshot = agent.tools.snapshot();
        tools.iter().all(|name| {
            name != "*"
                && (snapshot.resolve(name))
                    .is_some_and(|tool| tool.tool().scope() == ToolScope::Read)
        })
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if text(&input, "description").is_none_or(|d| d.trim().is_empty()) {
            return Err(ToolOutputError::new(
                "description must be a non-empty string",
            ));
        }
        if text(&input, "prompt").is_none_or(str::is_empty) {
            return Err(ToolOutputError::new("prompt must be a non-empty string"));
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!(
            "Spawn subagent: {}",
            text(input, "description").unwrap_or("")
        )
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let requested = text(&input, "subagent_type");
        let Some(definition) = self.roster.get(requested) else {
            let names: Vec<&str> = (self.roster.listed().iter())
                .map(|d| d.name.as_str())
                .collect();
            let message = format!(
                "Unknown subagent_type '{}'. Available: {}. Or omit subagent_type to use \
                 the built-in default.",
                requested.unwrap_or("").trim(),
                names.join(", ")
            );
            return Err(ToolOutputError::new(message));
        };
        let Some(agent) = self.agent() else {
            return Err(ToolOutputError::new("The agent of this tool is gone."));
        };
        let prompt = text(&input, "prompt").unwrap_or("").to_owned();
        run::run(&agent, definition, prompt, &ctx).await
    }
}
