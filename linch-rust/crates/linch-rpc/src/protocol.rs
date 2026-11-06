//! Method names and the typed params and results of each method, in both
//! directions. Every params object rejects unknown fields.

use std::collections::BTreeMap;

use linch::hooks::ApprovalResponse;
use linch::{Envelope, Principal, RunId, SessionId};
use serde::{Deserialize, Serialize};

use crate::error::ErrorObject;

mod call;
mod hook;
mod open;

pub use call::{CancelRequestParams, ToolCallParams, tool_code};
pub use hook::{
    CallbackSpec, ContextBuildAnswer, HookCallParams, PostToolUseAnswer, PostTurnAnswer,
    PreToolUseAnswer, StopAnswer, UserPromptAnswer, WireToolResult,
};
pub use open::{HostToolSpec, OpenParams};

/// Method and notification names.
pub mod method {
    /// Open an agent from an `AgentConfig` plus host tools and callbacks.
    pub const AGENT_OPEN: &str = "agent.open";
    /// Disconnect an agent's MCP servers and free its handle.
    pub const AGENT_CLOSE: &str = "agent.close";
    /// Start a run.
    pub const RUN_START: &str = "run.start";
    /// Resume a run.
    pub const RUN_RESUME: &str = "run.resume";
    /// Answer a suspended run's approvals and continue it.
    pub const RUN_APPROVE: &str = "run.approve";
    /// Cancel an active run.
    pub const RUN_CANCEL: &str = "run.cancel";
    /// Read a run's durable events after a seq.
    pub const RUN_EVENTS: &str = "run.events";
    /// Notification: one envelope of an active run.
    pub const RUN_EVENT: &str = "run.event";
    /// Notification: the server stopped driving a run.
    pub const RUN_END: &str = "run.end";
    /// Server request: run a host tool in the client.
    pub const TOOL_CALL: &str = "tool.call";
    /// Server request: one occurrence of a callback's hook point.
    pub const HOOK_CALL: &str = "hook.call";
    /// Notification from the server: it no longer waits for the answer to
    /// one of its requests.
    pub const CANCEL_REQUEST: &str = "$/cancelRequest";
}

/// Result of `agent.open`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentOpened {
    /// Handle naming the agent in later calls.
    pub agent: String,
    /// The agent's run contract digest, hex.
    pub contract: String,
}

/// Params of `agent.close`. The result is `{}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloseParams {
    /// Agent handle from `agent.open`.
    pub agent: String,
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
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
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
    /// The answer per pending `tool_use_id`. Answers for calls that are not
    /// pending are ignored; a pending call without one is asked again.
    pub decisions: BTreeMap<String, Decision>,
    /// Caller identity for permission rules.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub principal: Option<Principal>,
}

/// The answer to one pending tool call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "decision", rename_all = "snake_case", deny_unknown_fields)]
pub enum Decision {
    /// Run the call (later policy may still deny it).
    Allow {},
    /// Refuse the call; `reason` is shown to the model.
    Deny {
        /// Why the call was refused.
        reason: String,
    },
}

impl From<Decision> for ApprovalResponse {
    fn from(decision: Decision) -> Self {
        match decision {
            Decision::Allow {} => Self::Allow,
            Decision::Deny { reason } => Self::Deny { reason },
        }
    }
}

/// Result of `run.start`, `run.resume` and `run.approve`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunAccepted {
    /// The run the server now drives.
    pub run_id: RunId,
}

/// Params of `run.cancel`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelParams {
    /// The run to cancel.
    pub run_id: RunId,
}

/// Result of `run.cancel`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Cancelled {
    /// Whether the server was driving the run; `false` is not an error.
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
