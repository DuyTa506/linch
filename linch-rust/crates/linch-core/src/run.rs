//! Run and response status vocabularies shared by events and stores.

use serde::{Deserialize, Serialize};

/// Why a provider response stopped generating.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    /// The model finished its turn.
    EndTurn,
    /// The model asked for one or more tool calls.
    ToolUse,
    /// The output token limit was reached.
    MaxTokens,
    /// A configured stop sequence was produced.
    StopSequence,
    /// The model refused to answer.
    Refusal,
    /// The provider reported an error.
    Error,
    /// The worker lost its run lease mid-response.
    LeaseLost,
    /// The run was interrupted before the response completed.
    Interrupted,
}

/// Lifecycle status of a durable run.
///
/// The two `Awaiting*` states suspend a run without holding a process: the
/// run is resumed by an approval or an answer delivered through the API.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    /// Created and queued, not yet claimed.
    Pending,
    /// Executing under a lease.
    Running,
    /// Suspended until a pending tool permission is approved or denied.
    AwaitingApproval,
    /// Suspended until a question (ask-user, workflow interrupt) is answered.
    AwaitingInput,
    /// Finished successfully.
    Completed,
    /// Finished with an error.
    Failed,
    /// Stopped by an abort request.
    Aborted,
}

impl RunStatus {
    /// Whether the run has finished and can no longer change.
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::Aborted)
    }

    /// Whether the run is suspended waiting for an external answer.
    pub fn is_awaiting(self) -> bool {
        matches!(self, Self::AwaitingApproval | Self::AwaitingInput)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statuses_use_snake_case_wire_names() {
        assert_eq!(
            serde_json::to_string(&RunStatus::AwaitingApproval).unwrap(),
            "\"awaiting_approval\""
        );
        assert_eq!(
            serde_json::to_string(&StopReason::ToolUse).unwrap(),
            "\"tool_use\""
        );
        assert!(serde_json::from_str::<RunStatus>("\"suspended\"").is_err());
    }

    #[test]
    fn terminal_and_awaiting_are_disjoint() {
        use RunStatus::*;
        for status in [
            Pending,
            Running,
            AwaitingApproval,
            AwaitingInput,
            Completed,
            Failed,
            Aborted,
        ] {
            assert!(
                !(status.is_terminal() && status.is_awaiting()),
                "{status:?}"
            );
        }
        assert!(Completed.is_terminal() && AwaitingInput.is_awaiting() && !Running.is_terminal());
    }
}
