//! Method names and the typed params and results of each method, in both
//! directions. Every params object rejects unknown fields.

use std::collections::BTreeMap;
use std::fmt;

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
    /// The first message on a socket connection: present a bearer token.
    pub const CONNECTION_AUTH: &str = "connection.auth";
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
    /// Cancel an active run.
    pub const RUN_CANCEL: &str = "run.cancel";
    /// Read a run's durable events after a seq.
    pub const RUN_EVENTS: &str = "run.events";
    /// Notification: one or more envelopes of an active run, in order.
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

    use super::ApproveParams;

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
