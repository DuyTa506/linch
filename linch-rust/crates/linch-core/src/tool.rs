//! The tool contract shared by the registry, permissions, hooks and scheduler.
//!
//! A tool is any type implementing [`Tool`]; there is no base struct to embed.
//! Every tool states its [`ToolScope`] explicitly (no default), because the
//! scope decides both auto-approval and whether the call may run in parallel
//! (Linch 3.x issue #99: a defaulted `read` scope let side-effecting functions
//! run unapproved and in parallel).

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use crate::events::Event;
use crate::ids::{RunId, SessionId};
use crate::json::{JsonObject, JsonValue};
use crate::policy::Principal;

/// What a tool may touch. Only [`ToolScope::Read`] calls run in parallel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolScope {
    /// Reads state only; may run in parallel with other read calls.
    Read,
    /// Changes files or other state; runs alone.
    Write,
    /// Runs processes or other side effects; runs alone.
    Exec,
}

impl ToolScope {
    /// Whether calls with this scope may run concurrently.
    pub fn is_parallel_safe(self) -> bool {
        self == Self::Read
    }
}

/// Successful tool output, before it is rendered for a provider.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolOutput {
    /// Primary result value.
    pub value: JsonValue,
    /// Host-facing details that are not shown to the model.
    #[serde(default, skip_serializing_if = "JsonObject::is_empty")]
    pub metadata: JsonObject,
}

impl ToolOutput {
    /// Output holding `value` and no metadata.
    pub fn new(value: impl Into<JsonValue>) -> Self {
        Self {
            value: value.into(),
            metadata: JsonObject::new(),
        }
    }
}

/// An expected tool failure. The message is what the model sees.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolOutputError {
    /// Provider-facing error text.
    pub message: String,
    /// Machine-readable error code.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    /// Structured details for the host.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details: Option<JsonValue>,
}

impl ToolOutputError {
    /// Error with `message` and no code or details.
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: None,
            details: None,
        }
    }
}

/// Result of one tool execution.
pub type ToolResult = Result<ToolOutput, ToolOutputError>;

/// One progress report from a running tool.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolProgress {
    /// Human-readable progress message.
    pub message: String,
    /// Optional structured payload.
    pub data: Option<JsonObject>,
}

/// What a call the scheduler runs reports while it runs.
#[derive(Debug)]
pub(crate) enum Report {
    /// [`ToolContext::report_progress`].
    Progress(ToolProgress),
    /// An event for the run's log (a subagent's wrapped event).
    Event(Box<Event>),
}

/// Where a context's reports go.
#[derive(Debug, Clone)]
enum Reports {
    /// A host's progress sink ([`ToolContext::new`]).
    Progress(mpsc::UnboundedSender<ToolProgress>),
    /// The scheduler, which also takes events.
    Run(mpsc::UnboundedSender<Report>),
}

/// Runtime data for one tool call. Built by the scheduler.
#[derive(Debug, Clone)]
pub struct ToolContext {
    /// Session that owns the call.
    pub session_id: SessionId,
    /// Run executing the call.
    pub run_id: RunId,
    /// Provider call id of this tool use.
    pub tool_use_id: String,
    /// Caller identity, when the host supplied one.
    pub principal: Option<Principal>,
    /// Cancelled when the run aborts or the call times out.
    pub cancel: CancellationToken,
    /// When the scheduler's call timeout ends the call; `None` without one.
    ///
    /// At the deadline the scheduler drops the call and reports a generic
    /// timeout. A tool that can stop early with a useful partial result
    /// (Bash keeps the output so far) should finish a little before it.
    pub deadline: Option<Instant>,
    reports: Option<Reports>,
}

impl ToolContext {
    /// Context for one call, with no deadline. `progress` receives
    /// [`Self::report_progress`].
    pub fn new(
        session_id: SessionId,
        run_id: RunId,
        tool_use_id: impl Into<String>,
        principal: Option<Principal>,
        cancel: CancellationToken,
        progress: Option<mpsc::UnboundedSender<ToolProgress>>,
    ) -> Self {
        Self {
            session_id,
            run_id,
            tool_use_id: tool_use_id.into(),
            principal,
            cancel,
            deadline: None,
            reports: progress.map(Reports::Progress),
        }
    }

    /// This context with its progress and events going to `reports`.
    pub(crate) fn with_reports(mut self, reports: mpsc::UnboundedSender<Report>) -> Self {
        self.reports = Some(Reports::Run(reports));
        self
    }

    /// This context with `deadline` as the call's deadline.
    pub fn with_deadline(mut self, deadline: Option<Instant>) -> Self {
        self.deadline = deadline;
        self
    }

    /// Stable `run_id:tool_use_id` key for this call.
    ///
    /// A resumed run never re-runs a call that started durably: a call with a
    /// durable start and no durable end is reported as interrupted. The key
    /// lets a tool deduplicate a kernel-side retry of the same call within a
    /// run, since a retry keeps its `tool_use_id`.
    pub fn idempotency_key(&self) -> String {
        format!("{}:{}", self.run_id, self.tool_use_id)
    }

    /// Report progress to observers. Best effort: never fails the tool.
    pub fn report_progress(&self, message: impl Into<String>, data: Option<JsonObject>) {
        let progress = ToolProgress {
            message: message.into(),
            data,
        };
        match &self.reports {
            Some(Reports::Progress(sink)) => {
                let _ = sink.send(progress);
            }
            Some(Reports::Run(sink)) => {
                let _ = sink.send(Report::Progress(progress));
            }
            None => {}
        }
    }

    /// Put `event` in the run's log. Best effort: dropped outside a run,
    /// and once the call has ended.
    pub(crate) fn emit(&self, event: Event) {
        if let Some(Reports::Run(sink)) = &self.reports {
            let _ = sink.send(Report::Event(Box::new(event)));
        }
    }
}

/// A tool the model can call.
///
/// Implementations are `Send + Sync` and stateless per call; per-call data
/// arrives in [`ToolContext`]. `execute` must return promptly once
/// `ctx.cancel` fires.
#[async_trait]
pub trait Tool: Send + Sync {
    /// Name the model uses to call the tool.
    fn name(&self) -> &str;

    /// Description shown to the model.
    fn description(&self) -> &str;

    /// JSON Schema (draft 2020-12) for the input object.
    fn input_schema(&self) -> &JsonObject;

    /// What the tool may touch. Required: there is no default.
    fn scope(&self) -> ToolScope;

    /// JSON Schema the successful output value must satisfy, if any.
    fn output_schema(&self) -> Option<&JsonObject> {
        None
    }

    /// Checks beyond the schema, run after schema validation and again after
    /// a `pre_tool_use` hook rewrites the input. Returns the input to use.
    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        Ok(input)
    }

    /// Whether this call may run alongside other read calls. The default is
    /// [`ToolScope::is_parallel_safe`]: only a read-scope tool's calls. A
    /// tool says yes for a call that does only read work whatever its
    /// scope (a subagent whose tools all read).
    fn parallel(&self, input: &JsonObject) -> bool {
        let _ = input;
        self.scope().is_parallel_safe()
    }

    /// Whether the tool is offered even when tool search defers the others
    /// (see [`crate::tool_search`]). The default is no.
    fn always_load(&self) -> bool {
        false
    }

    /// One-line summary of a call for events and approval prompts.
    fn summarize(&self, input: &JsonObject) -> String {
        let _ = input;
        self.name().to_owned()
    }

    /// Run the tool on validated input.
    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_read_scope_is_parallel_safe() {
        assert!(ToolScope::Read.is_parallel_safe());
        assert!(!ToolScope::Write.is_parallel_safe());
        assert!(!ToolScope::Exec.is_parallel_safe());
        assert!(serde_json::from_str::<ToolScope>("\"admin\"").is_err());
    }

    #[test]
    fn idempotency_key_joins_run_and_call() {
        let ctx = ToolContext::new(
            SessionId::from("s"),
            RunId::from("r1"),
            "toolu_1",
            None,
            CancellationToken::new(),
            None,
        );
        assert_eq!(ctx.idempotency_key(), "r1:toolu_1");
        ctx.report_progress("no sink is fine", None);
    }

    struct Echo(JsonObject);

    #[async_trait]
    impl Tool for Echo {
        fn name(&self) -> &str {
            "echo"
        }
        fn description(&self) -> &str {
            "Echo the input."
        }
        fn input_schema(&self) -> &JsonObject {
            &self.0
        }
        fn scope(&self) -> ToolScope {
            ToolScope::Read
        }
        async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
            Ok(ToolOutput::new(JsonValue::Object(input)))
        }
    }

    #[tokio::test]
    async fn a_tool_runs_behind_a_shared_trait_object() {
        let tool: std::sync::Arc<dyn Tool> = std::sync::Arc::new(Echo(JsonObject::new()));
        let mut input = JsonObject::new();
        input.insert("x".into(), 1.into());
        let ctx = ToolContext::new(
            SessionId::from("s"),
            RunId::from("r"),
            "t",
            None,
            CancellationToken::new(),
            None,
        );
        let out = tool.execute(input.clone(), ctx).await.unwrap();
        assert_eq!(out.value, JsonValue::Object(input.clone()));
        assert_eq!(tool.summarize(&input), "echo");
        assert_eq!(tool.validate(input.clone()).unwrap(), input);
    }

    #[test]
    fn progress_reaches_the_sink() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let ctx = ToolContext::new(
            SessionId::from("s"),
            RunId::from("r"),
            "t",
            None,
            CancellationToken::new(),
            Some(tx),
        );
        ctx.report_progress("half", None);
        assert_eq!(rx.try_recv().unwrap().message, "half");
    }
}
