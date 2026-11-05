//! Suspension kinds: external input requests, the suspended-run marker,
//! and child-run lifecycle status.

use crate::ids::RunId;
use crate::run::RunStatus;
use serde::{Deserialize, Serialize};

/// Origin of an [`InputRequestEvent`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputRequestKind {
    /// A tool asked the user a question (the `ask_user` tool family).
    AskUser,
    /// A workflow step requested external input to resume.
    WorkflowInterrupt,
}

/// A request for external input, unifying ask-user and workflow interrupts
/// under the same suspend mechanism (INVENTORY Q10).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputRequestEvent {
    /// Identifier for the pending request, answered through the `answer`
    /// API call. Echoed in [`SuspendedEvent::pending_request_ids`].
    pub request_id: String,
    /// Origin of the request.
    pub kind: InputRequestKind,
    /// Human-readable question or prompt shown to the answerer.
    pub prompt: String,
}

/// Why a run suspended, matching [`RunStatus::is_awaiting`].
///
/// A narrower type than [`RunStatus`] on purpose: a suspended run can only
/// ever be awaiting approval or input, so that is the only state this type
/// can represent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SuspendStatus {
    /// Suspended until a pending tool permission is approved or denied.
    AwaitingApproval,
    /// Suspended until a pending input request is answered.
    AwaitingInput,
}

/// A run suspended without holding a process.
///
/// A suspended run's event stream ends with this event instead of a
/// [`ResultEvent`](super::ResultEvent); the run resumes when every pending
/// request is resolved through the `approve`/`answer` API.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SuspendedEvent {
    /// Why the run suspended.
    pub status: SuspendStatus,
    /// Identifiers of the requests the run is waiting on: pending
    /// [`PermissionRequestItem::tool_use_id`](super::PermissionRequestItem)s
    /// when `status` is `AwaitingApproval`, or pending
    /// [`InputRequestEvent::request_id`]s when `status` is `AwaitingInput`.
    pub pending_request_ids: Vec<String>,
}

/// A lifecycle status change on a child (subagent) run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChildRunStatusEvent {
    /// The child run whose status changed.
    pub child_run_id: RunId,
    /// The child's new status.
    pub status: RunStatus,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suspend_status_uses_the_run_status_wire_strings() {
        assert_eq!(
            serde_json::to_value(SuspendStatus::AwaitingApproval).unwrap(),
            serde_json::to_value(RunStatus::AwaitingApproval).unwrap(),
        );
        assert_eq!(
            serde_json::to_value(SuspendStatus::AwaitingInput).unwrap(),
            serde_json::to_value(RunStatus::AwaitingInput).unwrap(),
        );
    }
}
