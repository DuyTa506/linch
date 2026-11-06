//! Run and response status vocabularies shared by events and stores.

use serde::{Deserialize, Serialize};

use crate::message::Message;
use crate::policy::PermissionDecision;
use crate::usage::Usage;

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

/// A permission outcome recorded for one tool call, so a resumed or approved
/// run reuses it instead of asking again.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolDecisionRecord {
    /// Provider call id of the tool use.
    pub tool_use_id: String,
    /// The recorded decision.
    pub decision: PermissionDecision,
}

/// Where a running run is inside its turn. Terminal states live only in
/// [`RunStatus`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "phase", rename_all = "snake_case", deny_unknown_fields)]
pub enum Phase {
    /// The run was created; the user prompt may not be in the session yet.
    Started {
        /// The user message to append.
        prompt: Message,
    },
    /// A provider call is about to be dispatched.
    ProviderPending {
        /// Delivery attempt number for this turn, from 1.
        attempt: u32,
    },
    /// A response was received; it may not be in the session yet.
    AssistantReceived {
        /// The assembled assistant message.
        message: Message,
        /// Why the response stopped.
        stop_reason: StopReason,
    },
    /// The tool calls of the last response are being decided and run.
    ToolBatch {
        /// Decisions recorded so far.
        decisions: Vec<ToolDecisionRecord>,
        /// Last event seq before the batch; tool starts and ends follow it.
        after_seq: u64,
    },
    /// Suspended until the host approves or denies the pending calls.
    AwaitingApproval {
        /// Decisions recorded so far.
        decisions: Vec<ToolDecisionRecord>,
        /// Tool use ids that wait for a decision.
        pending: Vec<String>,
    },
    /// A user message is ready for the next turn: the tool results, or the
    /// feedback of a `stop` hook that kept the run going. It may not be in
    /// the session yet.
    ToolResults {
        /// The user message to append.
        message: Message,
    },
    /// A compaction summary call is about to be dispatched, before the
    /// turn's primary call.
    CompactionPending {
        /// Delivery attempt number of the summary call, from 1.
        attempt: u32,
    },
    /// A compaction summary was received; its projection may not be in the
    /// session yet. The projection is the provider view's first message,
    /// `summary`, then the view from `keep_from` on.
    Compacted {
        /// The summary as a user message.
        summary: Message,
        /// Index in the provider view where the kept turns start.
        keep_from: u64,
    },
}

/// Durable progress of a running run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Checkpoint {
    /// Turn index, from 0.
    pub turn: u32,
    /// Usage accumulated so far.
    pub usage: Usage,
    /// Whether every call so far reported usage.
    pub usage_complete: bool,
    /// Session log length this checkpoint expects.
    pub session_len: u64,
    /// Position inside the turn.
    pub phase: Phase,
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
    #[test]
    fn checkpoint_round_trips_strictly() {
        let cp = Checkpoint {
            turn: 1,
            usage: Usage::default(),
            usage_complete: true,
            session_len: 3,
            phase: Phase::ToolBatch {
                decisions: vec![ToolDecisionRecord {
                    tool_use_id: "t1".into(),
                    decision: PermissionDecision::Allow {},
                }],
                after_seq: 7,
            },
        };
        let wire = serde_json::to_value(&cp).unwrap();
        assert_eq!(wire["phase"]["phase"], "tool_batch");
        assert_eq!(
            serde_json::from_value::<Checkpoint>(wire.clone()).unwrap(),
            cp
        );
        let mut extra = wire;
        extra["phase"]["x"] = 1.into();
        assert!(serde_json::from_value::<Checkpoint>(extra).is_err());
    }
}
