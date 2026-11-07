//! The worker tools: `SubagentContinue` runs a kept worker again, and
//! `TaskStop` stops a background one.

use std::sync::{Arc, OnceLock, Weak};

use async_trait::async_trait;
use serde_json::json;

use super::workers::Worker;
use super::{CONTINUE_TOOL, STOP_TOOL, run};
use crate::agent::AgentInner;
use crate::ids::SessionId;
use crate::json::{JsonObject, JsonValue};
use crate::run::RunStatus;
use crate::tool::{Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

/// The agent a worker tool belongs to, set once it is built. Weak, since
/// the agent holds the registry that holds the tool.
pub(crate) type Handle = Arc<OnceLock<Weak<AgentInner>>>;

const CONTINUE_DESCRIPTION: &str =
    "Continue an existing worker subagent by id, reusing its full context.

Use SubagentContinue instead of spawning a new Subagent when:
- The worker has already gathered useful context you want to build on.
- You want the worker to refine or extend its prior output.
- You need a follow-up action in the same focused scope.

Provide the worker_id from the original Subagent result, or the display name.
The worker's full conversation history is preserved across continuations.";

const STOP_DESCRIPTION: &str = "Stop a running worker subagent by id.

The worker is cancelled immediately. Its handle remains in the worker
registry so it can be continued via SubagentContinue.
Use this when a worker has gone off-track or is no longer needed.";

fn object(value: JsonValue) -> JsonObject {
    let JsonValue::Object(object) = value else {
        unreachable!("the schema is an object")
    };
    object
}

/// The trimmed string at `field`; empty when absent.
fn text<'a>(input: &'a JsonObject, field: &str) -> &'a str {
    input
        .get(field)
        .and_then(JsonValue::as_str)
        .unwrap_or("")
        .trim()
}

fn agent(handle: &Handle) -> Result<Arc<AgentInner>, ToolOutputError> {
    let agent = handle.get().and_then(Weak::upgrade);
    agent.ok_or_else(|| ToolOutputError::new("The agent of this tool is gone."))
}

/// The addressable worker `target` of the calling session, or the
/// model-facing error `missing` builds from the known worker ids.
fn worker(
    agent: &AgentInner,
    ctx: &ToolContext,
    target: &str,
    missing: impl FnOnce(Vec<String>) -> String,
) -> Result<Worker, ToolOutputError> {
    let registry = &agent.worker_registry;
    match registry.resolve(&ctx.session_id, target) {
        Ok(Some(worker)) => Ok(worker),
        Ok(None) => Err(ToolOutputError::new(missing(registry.ids(&ctx.session_id)))),
        Err(ambiguous) => Err(ToolOutputError::new(ambiguous)),
    }
}

/// Runs a kept worker again on its own session, as a new foreground child
/// run of the calling run.
pub(crate) struct ContinueTool {
    agent: Handle,
    schema: JsonObject,
}

impl ContinueTool {
    pub(crate) fn new(agent: Handle) -> Self {
        let schema = object(json!({
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Worker id ('<parent run id>:<tool_use_id>', e.g. \
                                    'run-7:call_1') or display name of the retained worker to \
                                    continue. Use the id returned in the original Subagent \
                                    result.",
                },
                "message": {
                    "type": "string",
                    "description": "The follow-up message or task to send to the worker.",
                },
            },
            "required": ["to", "message"],
        }));
        Self { agent, schema }
    }
}

#[async_trait]
impl Tool for ContinueTool {
    fn name(&self) -> &str {
        CONTINUE_TOOL
    }

    fn description(&self) -> &str {
        CONTINUE_DESCRIPTION
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if text(&input, "to").is_empty() {
            let message = "'to' must be a non-empty string worker id or display name";
            return Err(ToolOutputError::new(message));
        }
        let message = input.get("message").and_then(JsonValue::as_str);
        if message.is_none_or(str::is_empty) {
            return Err(ToolOutputError::new("'message' must be a non-empty string"));
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Continue worker: {}", text(input, "to"))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let agent = agent(&self.agent)?;
        let target = text(&input, "to");
        let worker = worker(&agent, &ctx, target, |known| match known.is_empty() {
            true => format!(
                "No live worker '{target}'. No workers have been spawned yet. Use the \
                 Subagent tool to create one."
            ),
            false => format!(
                "No live worker '{target}'. Known worker ids: {}. Spawn a fresh Subagent if \
                 this worker is no longer needed.",
                known.join(", ")
            ),
        })?;
        let family = agent.family(&ctx.run_id);
        if family.is_some_and(|family| family.is_running(&worker.id)) {
            return Err(ToolOutputError::new(format!(
                "Worker '{}' is still running. Wait for its <task-notification>, or stop it \
                 with TaskStop.",
                worker.id
            )));
        }
        let message = input.get("message").and_then(JsonValue::as_str);
        let message = message.unwrap_or("").to_owned();
        let session = SessionId::new(worker.id.as_str());
        let outcome = run::run(&agent, &worker.definition, session, message, &ctx).await?;
        outcome.result("Worker")
    }
}

/// How a worker run ended, as `TaskStop` tells it; `None` while it has not.
pub(super) fn ended(status: RunStatus) -> Option<&'static str> {
    match status {
        RunStatus::Completed => Some("completed"),
        RunStatus::Failed => Some("failed"),
        RunStatus::Aborted => Some("killed"),
        _ => None,
    }
}

/// Stops a background worker; it stays addressable.
pub(crate) struct StopTool {
    agent: Handle,
    schema: JsonObject,
}

impl StopTool {
    pub(crate) fn new(agent: Handle) -> Self {
        let schema = object(json!({
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The worker id ('<parent run id>:<tool_use_id>', e.g. \
                                    'run-7:call_1') or display name to stop. Stopped workers \
                                    remain addressable via SubagentContinue.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for stopping the worker (logged to the \
                                    handle).",
                },
            },
            "required": ["task_id"],
        }));
        Self { agent, schema }
    }
}

#[async_trait]
impl Tool for StopTool {
    fn name(&self) -> &str {
        STOP_TOOL
    }

    fn description(&self) -> &str {
        STOP_DESCRIPTION
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Exec
    }

    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if text(&input, "task_id").is_empty() {
            return Err(ToolOutputError::new("'task_id' must be a non-empty string"));
        }
        Ok(input)
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("Stop worker: {}", text(input, "task_id"))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let agent = agent(&self.agent)?;
        let target = text(&input, "task_id");
        let worker = worker(&agent, &ctx, target, |known| match known.is_empty() {
            true => format!("No worker '{target}' found. No workers spawned yet."),
            false => format!(
                "No worker '{target}' found. Known workers: {}.",
                known.join(", ")
            ),
        })?;
        let family = agent.family(&ctx.run_id);
        if !family
            .as_ref()
            .is_some_and(|family| family.is_running(&worker.id))
        {
            // Nothing to stop: say how the worker ended instead.
            let record = agent.runs.load(&agent.namespace, &worker.id).await;
            if let Some(ended) = record.ok().and_then(|record| ended(record.status)) {
                return Ok(ToolOutput::new(format!(
                    "Worker '{}' ({}) already finished ({ended}). Use SubagentContinue to \
                     re-engage if needed.",
                    worker.id, worker.display_name
                )));
            }
        }
        if let Some(family) = family {
            family.stop(&worker.id);
        }
        let mut message = format!("Worker '{}' ({}) stopped.", worker.id, worker.display_name);
        let reason = input.get("reason").and_then(JsonValue::as_str);
        if let Some(reason) = reason.filter(|reason| !reason.is_empty()) {
            let end = if reason.ends_with(['.', '!', '?']) {
                ""
            } else {
                "."
            };
            message.push_str(&format!(" Reason: {reason}{end}"));
        }
        message.push_str(" Use SubagentContinue to re-engage if needed.");
        Ok(ToolOutput::new(message))
    }
}
