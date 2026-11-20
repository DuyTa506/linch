//! Method names and the typed params and results of each method, in both
//! directions. Every params object rejects unknown fields.

use std::collections::BTreeMap;
use std::fmt;

use linch::ask_user::AskUserAnswer;
use linch::hooks::ApprovalResponse;
use linch::{Envelope, OutputSchema, Principal, RunId, SessionId, ToolChoice};
use serde::{Deserialize, Serialize};

use crate::error::ErrorObject;

mod call;
mod exec;
mod hook;
mod info;
pub mod monitor;
mod open;
mod schedule;
mod workflow;

pub use call::{CancelRequestParams, ToolCallParams, tool_code};
pub use exec::{ExecKillParams, ExecReadParams, ExecStartParams};
pub use hook::{
    AfterProviderCallAnswer, BeforeProviderCallAnswer, CallbackSpec, ContextBuildAnswer,
    HookCallParams, ObserveAnswer, PostToolUseAnswer, PostTurnAnswer, PreToolUseAnswer, StopAnswer,
    ToolExecuteAnswer, UserPromptAnswer, WireToolResult,
};
pub use info::{BACKENDS, CAPABILITIES, InfoLimits, InfoParams, PROTOCOL_VERSION, ServerInfo};
pub use monitor::{EvalReport, EvalRunParams, ReportParams, RunReportResult};
pub use open::{HostToolSpec, OpenParams};
pub use schedule::{
    JobEnqueueParams, JobGetParams, JobInfo, JobListParams, Jobs, ScheduleCreateParams,
    ScheduleDeleted, ScheduleIdParams, ScheduleInfo, ScheduleListParams, Schedules,
};

pub use workflow::{
    MAX_PENDING_STEPS, MAX_WORKFLOW_ID_BYTES, WireRetry, WorkflowAgentParams, WorkflowAgentResult,
    WorkflowBudget, WorkflowCancelParams, WorkflowCancelled, WorkflowEventParams,
    WorkflowFinishParams, WorkflowFinished, WorkflowInterruptParams, WorkflowInterrupted,
    WorkflowLimits, WorkflowStartParams, WorkflowStarted, WorkflowStatus, WorkflowStatusParams,
    WorkflowStepAbandonParams, WorkflowStepAbandoned, WorkflowStepBeginParams, WorkflowStepBegun,
    WorkflowStepCompleteParams,
};

/// Method and notification names.
pub mod method {
    /// The first message on a socket connection: present a bearer token.
    pub const CONNECTION_AUTH: &str = "connection.auth";
    /// What the server is: its versions, capabilities and main limits. The
    /// first call a client makes after connecting.
    pub const SERVER_INFO: &str = "server.info";
    /// Open an agent from an `AgentConfig` plus host tools and callbacks.
    pub const AGENT_OPEN: &str = "agent.open";
    /// Disconnect an agent's MCP servers and free its handle.
    pub const AGENT_CLOSE: &str = "agent.close";
    /// Tell an agent the client is done with a session.
    pub const SESSION_CLOSE: &str = "session.close";
    /// Start a run.
    pub const RUN_START: &str = "run.start";
    /// Resume a run.
    pub const RUN_RESUME: &str = "run.resume";
    /// Answer a suspended run's approvals and continue it.
    pub const RUN_APPROVE: &str = "run.approve";
    /// Answer a run's pending `AskUser` questions and continue it.
    pub const RUN_ANSWER: &str = "run.answer";
    /// Queue text for a live run to read at its next turn boundary.
    pub const RUN_STEER: &str = "run.steer";
    /// Cancel an active run.
    pub const RUN_CANCEL: &str = "run.cancel";
    /// Read a run's durable events after a seq.
    pub const RUN_EVENTS: &str = "run.events";
    /// Store a schedule that runs the agent in the background.
    pub const SCHEDULE_CREATE: &str = "schedule.create";
    /// List the schedules of the agent's namespace.
    pub const SCHEDULE_LIST: &str = "schedule.list";
    /// Stop a schedule's fires.
    pub const SCHEDULE_PAUSE: &str = "schedule.pause";
    /// Let a paused schedule fire again, from now.
    pub const SCHEDULE_RESUME: &str = "schedule.resume";
    /// Delete a schedule; its queued and running jobs go on.
    pub const SCHEDULE_DELETE: &str = "schedule.delete";
    /// Queue a one-off run in the background.
    pub const JOB_ENQUEUE: &str = "job.enqueue";
    /// Read one job's state.
    pub const JOB_GET: &str = "job.get";
    /// List the jobs of the agent's namespace, newest first.
    pub const JOB_LIST: &str = "job.list";
    /// Start a workflow, or resume it from its journal.
    pub const WORKFLOW_START: &str = "workflow.start";
    /// One journaled agent call of a workflow.
    pub const WORKFLOW_AGENT: &str = "workflow.agent";
    /// Begin a journaled step whose function runs in the client.
    pub const WORKFLOW_STEP_BEGIN: &str = "workflow.step.begin";
    /// Journal the value of a step begun with `workflow.step.begin`.
    pub const WORKFLOW_STEP_COMPLETE: &str = "workflow.step.complete";
    /// Give back a ticket of `workflow.step.begin` whose function failed.
    pub const WORKFLOW_STEP_ABANDON: &str = "workflow.step.abandon";
    /// Park the workflow at a question, or read its journaled answer.
    pub const WORKFLOW_INTERRUPT: &str = "workflow.interrupt";
    /// Abort one agent call of a workflow, or all of them.
    pub const WORKFLOW_CANCEL: &str = "workflow.cancel";
    /// End the client's drive of a workflow and store its status.
    pub const WORKFLOW_FINISH: &str = "workflow.finish";
    /// Read a stored workflow's status.
    pub const WORKFLOW_STATUS: &str = "workflow.status";
    /// Notification: one event of a workflow this connection drives.
    pub const WORKFLOW_EVENT: &str = "workflow.event";
    /// What a provider supports for a model, from a provider config.
    pub const PROVIDER_CAPABILITIES: &str = "provider.capabilities";
    /// The models a provider's server lists.
    pub const PROVIDER_MODELS: &str = "provider.models";
    /// One model of a provider's list.
    pub const PROVIDER_MODEL: &str = "provider.model";
    /// Notification: one or more envelopes of an active run, in order.
    pub const RUN_EVENT: &str = "run.event";
    /// Notification: the server stopped driving a run.
    pub const RUN_END: &str = "run.end";
    /// Server request: run a host tool in the client.
    pub const TOOL_CALL: &str = "tool.call";
    /// Server request: one occurrence of a callback's hook point.
    pub const HOOK_CALL: &str = "hook.call";
    /// Server request: start a command of a `remote` execution backend in
    /// the client.
    pub const EXEC_START: &str = "exec.start";
    /// Server request: take the new output of such a command, waiting a
    /// while for some.
    pub const EXEC_READ: &str = "exec.read";
    /// Server request: kill such a command and answer once it is dead.
    pub const EXEC_KILL: &str = "exec.kill";
    /// Notification from the server: it no longer waits for the answer to
    /// one of its requests.
    pub const CANCEL_REQUEST: &str = "$/cancelRequest";
}

/// Params of `connection.auth`.
///
/// `Debug` hides the token.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthParams {
    /// A bearer token from the server's config.
    pub token: String,
}

impl fmt::Debug for AuthParams {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AuthParams")
            .field("token", &"[redacted]")
            .finish()
    }
}

/// Result of `connection.auth`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Authenticated {
    /// The tenant the token belongs to; the connection now acts for it.
    pub tenant: String,
}

/// Result of `agent.open`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentOpened {
    /// Handle naming the agent in later calls.
    pub agent: String,
    /// The agent's run contract digest, hex.
    pub contract: String,
    /// The skill directories and subagent files of the config directory
    /// that were left out; absent when none was.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub skipped: Vec<Skipped>,
}

/// A skill directory or subagent file `agent.open` left out
/// ([`linch::project::Skipped`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Skipped {
    /// The skill directory or subagent file.
    pub path: String,
    /// Why: `missing_skill_md`, `missing_frontmatter`,
    /// `invalid_frontmatter`, `io_error`, `name_collision_tool`,
    /// `name_collision_skill` or `name_collision`
    /// ([`linch::project::SkipReason`]).
    pub reason: String,
    /// What exactly is wrong, for a person to read.
    pub detail: String,
}

impl From<&linch::project::Skipped> for Skipped {
    fn from(skipped: &linch::project::Skipped) -> Self {
        use linch::project::SkipReason;
        let reason = match skipped.reason {
            SkipReason::MissingSkillMd => "missing_skill_md",
            SkipReason::MissingFrontmatter => "missing_frontmatter",
            SkipReason::InvalidFrontmatter => "invalid_frontmatter",
            SkipReason::IoError => "io_error",
            SkipReason::NameCollisionTool => "name_collision_tool",
            SkipReason::NameCollisionSkill => "name_collision_skill",
            SkipReason::NameCollision => "name_collision",
        };
        Self {
            path: skipped.path.display().to_string(),
            reason: reason.into(),
            detail: skipped.detail.clone(),
        }
    }
}

/// Params of `agent.close`. The result is `{}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloseParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
}

/// Params of `provider.models`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderParams {
    /// The provider, as an agent config's `provider`: its `id` and
    /// `base_url`. The key comes from the server's environment.
    pub provider: linch::ProviderConfig,
}

/// Params of `provider.capabilities` and `provider.model`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderModelParams {
    /// The provider, as an agent config's `provider`.
    pub provider: linch::ProviderConfig,
    /// The model asked about.
    pub model: String,
}

/// Params of `session.close`. The result is `{}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionCloseParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// The session the client is done with.
    pub session_id: SessionId,
}

/// Params of `run.start`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// Session the run belongs to; created when missing.
    pub session_id: SessionId,
    /// The user prompt.
    pub prompt: String,
    /// Id for the new run; the server picks a fresh one when absent. An id
    /// the connection drives is refused with `-32002`; an id already stored
    /// ends the run with `-32023` in `run.end`, and nothing is written.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<RunId>,
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
    /// The host's trace id, stored with the run and echoed on its `init`
    /// and `result` events, its tool calls and its subagents' runs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    /// The output schema of this run alone, in place of the agent's
    /// `spec.output_schema`: `{name, schema, strict?, description?}`. It
    /// is stored with the run, so a resume or approve uses it again. An
    /// unusable one ends the run with `-32031` and writes nothing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_schema: Option<OutputSchema>,
    /// The tool choice of this run alone, in place of the agent's
    /// `spec.tool_choice`: `{"type": "auto" | "none" | "required"}` or
    /// `{"type": "tool", "name"}`. It is stored with the run, so a resume or
    /// approve uses it again. One that names a tool the run does not offer,
    /// or that the provider refuses, ends the run with `-32034` and writes
    /// nothing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_choice: Option<ToolChoice>,
}

/// Params of `run.resume`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResumeParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// The run to continue.
    pub run_id: RunId,
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `run.approve`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApproveParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// A run suspended awaiting approval.
    pub run_id: RunId,
    /// The answer per pending `tool_use_id`, in the core wire form:
    /// `{"decision": "allow"}` or `{"decision": "deny", "reason": "..."}`.
    /// Answers for calls that are not pending are ignored; a pending call
    /// without one is asked again.
    pub decisions: BTreeMap<String, ApprovalResponse>,
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Params of `run.answer`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnswerParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// A run suspended awaiting input.
    pub run_id: RunId,
    /// The user's answer per pending `request_id` (an `input_request`'s,
    /// the `AskUser` call's `tool_use_id`), in the core wire form:
    /// `{"accepted"?: bool, "answers": {question_id: "label" | ["label", ...]},
    /// "note"?: "..."}`. At least one; a pending request left out is asked
    /// again.
    pub answers: BTreeMap<String, AskUserAnswer>,
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Result of `run.start`, `run.resume`, `run.approve` and `run.answer`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunAccepted {
    /// The run the server now drives.
    pub run_id: RunId,
}

/// Params of `run.steer`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SteerParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// The run to steer: running, or suspended for an approval or an answer.
    pub run_id: RunId,
    /// What the model reads; not blank, at most [`linch::MAX_STEER_BYTES`]
    /// bytes.
    pub text: String,
    /// Names the steer, so a retried call queues it once; absent: the server
    /// picks one. Letters, digits and `-_.:`, at most 128 bytes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// Caller identity, checked against the tenant's principal rules as for
    /// the other run methods.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// Result of `run.steer`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Steered {
    /// Always `true`: the steer is durable in the run's journal.
    pub queued: bool,
    /// The steer's id, named by the `alignment` `user` event that delivers
    /// it in `steer_id`.
    pub id: String,
}

/// Params of `run.cancel`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelParams {
    /// The run to cancel.
    pub run_id: RunId,
    /// Agent handle from `agent.open`. With it, a run this connection does
    /// not drive is cancelled through the agent's stores when it waits for
    /// an approval or an answer ([`linch::Agent::cancel`]); without it,
    /// only a driven run is.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
}

/// Result of `run.cancel`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Cancelled {
    /// Whether the run was cancelled: the server was driving it, or the
    /// cancel ended it through the stores; `false` is not an error.
    pub cancelled: bool,
}

/// Params of `run.events`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventsParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
    /// The run to read.
    pub run_id: RunId,
    /// Return the envelopes with a greater seq (default 0: all).
    #[serde(default)]
    pub after_seq: u64,
}

/// Result of `run.events`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Events {
    /// Durable envelopes in seq order.
    pub events: Vec<Envelope>,
}

/// Params of the `run.event` notification: consecutive envelopes of one
/// run, in the run's order. Each envelope is whole (`v`, `seq`, `run_id`,
/// `ts_ms`, `event`) and decodes on its own.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunEvent {
    /// The run every envelope belongs to.
    pub run_id: RunId,
    /// One or more envelopes.
    pub envelopes: Vec<Envelope>,
}

/// Params of the `run.end` notification.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunEnd {
    /// The run the server stopped driving.
    pub run_id: RunId,
    /// Why the run could not continue durably; absent when its stream
    /// ended normally (after its `result` or `suspended` envelope).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorObject>,
}

#[cfg(test)]
mod tests {
    use linch::hooks::ApprovalResponse;
    use serde_json::json;

    use super::{ApproveParams, StartParams};

    #[test]
    fn start_params_carry_an_output_schema_only_when_given() {
        let bare = json!({"agent": "a", "session_id": "s1", "prompt": "hi"});
        let params: StartParams = serde_json::from_value(bare.clone()).unwrap();
        assert_eq!(params.output_schema, None);
        assert_eq!(serde_json::to_value(&params).unwrap(), bare);
        let mut shaped = bare.clone();
        shaped["output_schema"] = json!({"name": "invoice", "schema": {"type": "object"}});
        let params: StartParams = serde_json::from_value(shaped).unwrap();
        let schema = params.output_schema.unwrap();
        assert_eq!((schema.name.as_str(), schema.strict), ("invoice", true));
        let mut wrong = bare;
        wrong["output_schema"] = json!({"name": "x", "schema": {}, "format": "json"});
        assert!(serde_json::from_value::<StartParams>(wrong).is_err());
    }

    #[test]
    fn start_params_carry_a_tool_choice_only_when_given() {
        let bare = json!({"agent": "a", "session_id": "s1", "prompt": "hi"});
        let params: StartParams = serde_json::from_value(bare.clone()).unwrap();
        assert_eq!(params.tool_choice, None);
        let mut chosen = bare.clone();
        chosen["tool_choice"] = json!({"type": "tool", "name": "Read"});
        let params: StartParams = serde_json::from_value(chosen.clone()).unwrap();
        let read = linch::ToolChoice::Tool {
            name: "Read".into(),
        };
        assert_eq!(params.tool_choice, Some(read));
        assert_eq!(serde_json::to_value(&params).unwrap(), chosen);
        let mut wrong = bare;
        wrong["tool_choice"] = json!("required");
        assert!(serde_json::from_value::<StartParams>(wrong).is_err());
    }

    #[test]
    fn approve_params_carry_core_approval_responses_strictly() {
        let wire = json!({"agent": "agent-1", "run_id": "r1", "decisions": {
            "c1": {"decision": "allow"},
            "c2": {"decision": "deny", "reason": "not now"},
        }});
        let params: ApproveParams = serde_json::from_value(wire.clone()).unwrap();
        assert_eq!(params.decisions["c1"], ApprovalResponse::Allow);
        let deny = ApprovalResponse::Deny {
            reason: "not now".into(),
        };
        assert_eq!(params.decisions["c2"], deny);
        assert_eq!(serde_json::to_value(&params).unwrap(), wire);
        for bad in [
            json!({"decision": "allow", "reason": "x"}),
            json!({"decision": "deny"}),
            json!({"decision": "maybe"}),
        ] {
            let wire = json!({"agent": "agent-1", "run_id": "r1", "decisions": {"c1": bad}});
            assert!(serde_json::from_value::<ApproveParams>(wire).is_err());
        }
    }
}
