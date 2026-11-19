//! Run and response status vocabularies shared by events and stores.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::json::JsonObject;
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
        /// The feedback of an `after_provider_call` hook that asked for
        /// another turn instead of acting on this response: none of its
        /// tool calls run, and the feedback goes back to the model next.
        /// Durable with the response, so a resumed run never asks the hook
        /// again. Absent when no hook asked, which keeps the checkpoint as
        /// it was before hooks could.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        retry_feedback: Option<String>,
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
    /// Suspended until the host answers the pending `AskUser` calls
    /// (status `awaiting_input`). Same shape as `AwaitingApproval`: the
    /// answers are committed as the calls' results, not here.
    AwaitingInput {
        /// Decisions recorded so far.
        decisions: Vec<ToolDecisionRecord>,
        /// Tool use ids of the `AskUser` calls that wait for an answer;
        /// each is its `input_request`'s `request_id`.
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
    /// Micro-compaction cleared the tool results before index `before` of
    /// the provider view (see [`crate::compaction`]); its projection may
    /// not be in the session yet. Delivery `attempt` of the turn's primary
    /// call follows: 1 at a turn's start, the next one after a prompt the
    /// provider refused as too long. Written only by an agent whose
    /// compaction sets `micro`, or, with `clip`, after a refusal the kept
    /// turns are too long for (see [`crate::compaction`]).
    Cleared {
        /// Index in the provider view where the kept turns start; with
        /// `clip`, where the tool results to clip start.
        before: u64,
        /// Delivery attempt of the primary call that follows, from 1.
        attempt: u32,
        /// Set when the tool results from `before` on were clipped instead of
        /// cleared: the budget in estimated tokens they were clipped to.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        clip: Option<u64>,
    },
    /// The run was cancelled (see [`crate::Agent::cancel`]): `message`,
    /// the results that pair the last response's tool calls, goes into
    /// the session, then the run ends `aborted`. It may not be in the
    /// session yet. Every continuation of the run finishes the abort; an
    /// approve or an answer gets `NotSuspended`.
    Aborting {
        /// The user message to append.
        message: Message,
    },
    /// A response called the run's final tool and the run ends on it:
    /// `message`, the results that pair the response's tool calls (the
    /// acknowledgement of an answer taken, or the error of one refused),
    /// goes into the session, then the run ends as `ending` says. It may
    /// not be in the session yet. Every continuation of the run appends it
    /// and ends the run, without asking the model or a hook again.
    Finishing {
        /// The user message to append.
        message: Message,
        /// How the run ends.
        ending: Ending,
    },
}

/// How a [`Phase::Finishing`] run ends: its status and its `Result`'s
/// answer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ending {
    /// `completed`, `failed`, or `aborted` (a cancel or a hook ended the run
    /// before it took the answer).
    pub status: RunStatus,
    /// The `stop_reason` of the `Result`.
    pub stop_reason: StopReason,
    /// The `final_text` of the `Result`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_text: Option<String>,
    /// The `structured_output` of the `Result`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_output: Option<JsonObject>,
    /// The `structured_error` of the `Result`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structured_error: Option<String>,
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
    /// Cost in USD of the calls so far that had a price; `None` until one
    /// had. Encoding a NaN or infinite value is an error.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        serialize_with = "crate::events::serialize_finite_f64_option"
    )]
    pub cost_usd: Option<f64>,
    /// `usage` split by the model that served each call; its sum is
    /// `usage`. Empty until a call reported usage.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub usage_by_model: BTreeMap<String, Usage>,
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
    /// Micro-compaction adds a phase; every phase written before it keeps
    /// its wire shape.
    #[test]
    fn a_cleared_view_is_a_phase_of_its_own() {
        let phase = Phase::Cleared {
            before: 5,
            attempt: 2,
            clip: None,
        };
        let wire = serde_json::json!({"phase": "cleared", "before": 5, "attempt": 2});
        assert_eq!(serde_json::to_value(&phase).unwrap(), wire);
        assert_eq!(serde_json::from_value::<Phase>(wire).unwrap(), phase);
        let clipped = Phase::Cleared {
            before: 5,
            attempt: 2,
            clip: Some(4000),
        };
        let wire = serde_json::json!({"phase": "cleared", "before": 5, "attempt": 2, "clip": 4000});
        assert_eq!(serde_json::to_value(&clipped).unwrap(), wire);
        assert_eq!(serde_json::from_value::<Phase>(wire).unwrap(), clipped);
        let compacted = serde_json::json!({
            "phase": "compacted",
            "summary": Message::user_text("s"),
            "keep_from": 3,
        });
        let decoded = serde_json::from_value::<Phase>(compacted.clone()).unwrap();
        assert_eq!(serde_json::to_value(decoded).unwrap(), compacted);
        let pending = serde_json::json!({"phase": "compaction_pending", "attempt": 1});
        let decoded = serde_json::from_value::<Phase>(pending.clone()).unwrap();
        assert_eq!(serde_json::to_value(decoded).unwrap(), pending);
    }

    #[test]
    fn checkpoint_round_trips_strictly() {
        let cp = Checkpoint {
            turn: 1,
            usage: Usage::default(),
            usage_complete: true,
            usage_by_model: Default::default(),
            cost_usd: None,
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
        assert!(
            wire.get("usage_by_model").is_none(),
            "an unset breakdown stays off the wire"
        );
        assert_eq!(wire["phase"]["phase"], "tool_batch");
        assert_eq!(
            serde_json::from_value::<Checkpoint>(wire.clone()).unwrap(),
            cp
        );
        let mut extra = wire;
        extra["phase"]["x"] = 1.into();
        assert!(serde_json::from_value::<Checkpoint>(extra).is_err());
    }

    /// A response no hook retried keeps the checkpoint written before hooks
    /// could retry, so those runs still resume and nothing else changes.
    #[test]
    fn a_received_response_without_a_retry_keeps_its_old_wire_shape() {
        let phase = |retry_feedback| Phase::AssistantReceived {
            message: Message::assistant_text("hi"),
            stop_reason: StopReason::EndTurn,
            retry_feedback,
        };
        let old = serde_json::json!({
            "phase": "assistant_received",
            "message": Message::assistant_text("hi"),
            "stop_reason": "end_turn",
        });
        assert_eq!(serde_json::to_value(phase(None)).unwrap(), old);
        assert_eq!(serde_json::from_value::<Phase>(old).unwrap(), phase(None));
        let retried = serde_json::to_value(phase(Some("again".into()))).unwrap();
        assert_eq!(retried["retry_feedback"], "again");
        assert_eq!(
            serde_json::from_value::<Phase>(retried).unwrap(),
            phase(Some("again".into()))
        );
    }
}
