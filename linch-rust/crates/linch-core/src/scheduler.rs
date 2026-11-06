//! Tool-batch planning and execution.
//!
//! The tool calls of one assistant response go through two calls, so the run
//! loop can suspend between them:
//!
//! 1. [`plan`] decides every call without running anything: resolve the name
//!    in the turn's [`ToolSnapshot`](crate::ToolSnapshot), validate the input,
//!    run the `pre_tool_use` chain, validate its final input again, evaluate
//!    permission (and the `permission_request` chain on `Ask`), then apply any
//!    [`RecordedApproval`] from an earlier suspend. When a call still needs
//!    approval, [`BatchPlan::is_awaiting_approval`] says so and the loop
//!    suspends the run.
//! 2. [`execute`] runs a decided plan and returns one [`ToolResultBlock`] per
//!    call, in call order. A contiguous run of read-scope calls executes
//!    concurrently, up to [`ExecuteOptions::max_concurrency`]; a write or exec
//!    call runs alone. Each tool start waits on [`ToolSink::tool_starting`],
//!    the durability barrier, and each result goes to
//!    [`ToolSink::tool_finished`] as soon as it exists.
//!
//! A `pre_tool_use` cached result is served only when permission allows the
//! call on its final input: a deny discards it, and an ask suspends as usual.
//!
//! [`ToolResultBlock`]: crate::ToolResultBlock

mod execute;
mod plan;

#[cfg(test)]
mod tests;

use crate::hooks::ApprovalResponse;
use crate::ids::{RunId, SessionId};
use crate::json::JsonObject;
use crate::policy::Principal;

pub use execute::{CallOutcome, ExecuteOptions, ToolSink, execute};
pub use plan::{BatchPlan, PlannedAction, PlannedCall, plan};

/// The run a batch belongs to. Every hook request and [`ToolContext`] in the
/// batch carries these values.
///
/// [`ToolContext`]: crate::ToolContext
#[derive(Debug, Clone, PartialEq)]
pub struct RunIdentity {
    /// Session that owns the run.
    pub session_id: SessionId,
    /// Run executing the batch.
    pub run_id: RunId,
    /// Caller identity, when the host supplied one. Permission rules match
    /// on its roles.
    pub principal: Option<Principal>,
}

/// A host's answer to a call that was awaiting approval, kept so a resumed
/// run can apply it when it plans the same batch again.
///
/// [`plan`] looks it up by `tool_use_id`. A deny applies whatever the
/// call's final input is (fail closed). An allow applies only while the
/// final input equals `input`: hooks run again on resume, and an allow
/// covers the input the host saw, not whatever a hook produces next time. A
/// mismatch leaves the call asking again.
#[derive(Debug, Clone, PartialEq)]
pub struct RecordedApproval {
    /// The input the host approved or denied ([`PlannedCall::input`]).
    pub input: JsonObject,
    /// The host's answer.
    pub response: ApprovalResponse,
}
