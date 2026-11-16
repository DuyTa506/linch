//! Resume reconciliation: what a crashed or suspended run does next, read
//! only from its durable checkpoint, events and session log (13.1.4).

use thiserror::Error;

use crate::events::model_call::{
    CallDisposition, ModelCallDeliveryEvent, ModelCallKind, ModelCallReceiptEvent,
};
use crate::events::{Envelope, Event};
use crate::message::{Message, Role, ToolResultBlock, ToolUseBlock};
use crate::run::{Checkpoint, Phase, StopReason, ToolDecisionRecord};
use crate::session_log::LogEntry;

mod session;
#[cfg(test)]
mod tests;

use crate::compaction::Forced;
use session::{appended, cleared, projected, prompt_appended, unchanged};

/// Result text for a tool that started but has no durable end.
pub const INTERRUPTED_TOOL_TEXT: &str = "Tool execution was interrupted before a result was \
     recorded; not re-running automatically on resume.";

/// What a resumed run does next. `appended` says whether the phase's pending
/// session message is already in the session log.
#[derive(Debug, Clone, PartialEq)]
pub enum Recovery {
    /// `Started`: append the prompt unless `appended`, then dispatch.
    ///
    /// A session that grew past the checkpoint's `session_len` by entries
    /// other than the prompt is not a conflict: runs that ended before this
    /// one was created appended them (the store admits one live run per
    /// session). `appended` is then false, and the prompt goes at the end.
    Started {
        /// The user prompt.
        prompt: Message,
        /// Whether the prompt is already in the session.
        appended: bool,
    },
    /// `ProviderPending`: write `abandoned`, if any, then dispatch
    /// `attempt`. The delivery may have a receipt already: a failed or
    /// cancelled one, or an accepted one whose response was never written
    /// (an orphan: the run crashed in a hook that follows the stream). Also
    /// `CompactionPending` whose summary call settled without a summary (it
    /// failed, or gave no text): the turn goes on uncompacted with its
    /// primary call, attempt 1.
    Redispatch {
        /// The new delivery attempt number.
        attempt: u32,
        /// A cancelled receipt for the unsettled delivery; `None` when the
        /// delivery already has a receipt.
        abandoned: Option<ModelCallReceiptEvent>,
    },
    /// A response is durable (`AssistantReceived`): append it unless
    /// `appended`, then go on.
    Received {
        /// The assistant message.
        message: Message,
        /// Why the response stopped.
        stop_reason: StopReason,
        /// Whether the message is already in the session.
        appended: bool,
        /// The feedback of an `after_provider_call` hook that asked for
        /// another turn instead of acting on the message (see
        /// [`Phase::AssistantReceived`]).
        retry_feedback: Option<String>,
    },
    /// `ToolBatch`: every call of the last assistant message, in order.
    ToolBatch {
        /// Decisions recorded so far, for the re-plan.
        decisions: Vec<ToolDecisionRecord>,
        /// How each call recovers.
        calls: Vec<ToolRecovery>,
    },
    /// `AwaitingApproval`: still waiting for the host's decisions.
    AwaitingApproval {
        /// Decisions recorded so far.
        decisions: Vec<ToolDecisionRecord>,
        /// Tool use ids that wait for a decision.
        pending: Vec<String>,
    },
    /// `AwaitingInput`: still waiting for the user's answers.
    AwaitingInput {
        /// Decisions recorded so far.
        decisions: Vec<ToolDecisionRecord>,
        /// Tool use ids of the `AskUser` calls that wait for an answer.
        pending: Vec<String>,
    },
    /// `ToolResults`: append the message unless `appended`, then go on.
    ToolResults {
        /// The user message to append (tool results or stop feedback).
        message: Message,
        /// Whether the message is already in the session.
        appended: bool,
    },
    /// `CompactionPending` without a receipt: write `abandoned`, then
    /// dispatch summary `attempt`.
    Recompact {
        /// The new delivery attempt number.
        attempt: u32,
        /// A cancelled receipt for the unsettled delivery.
        abandoned: Option<ModelCallReceiptEvent>,
        /// Set when the provider refused the turn's primary call as too
        /// long, which forced this summary: the primary call's delivery
        /// attempt that follows it. `None` at a turn's start.
        redispatch: Option<u32>,
    },
    /// `Compacted`: append the projection unless `appended`, then dispatch
    /// the turn's primary call.
    Compacted {
        /// The summary as a user message.
        summary: Message,
        /// Index in the provider view where the kept turns start.
        keep_from: u64,
        /// Whether the projection is already in the session.
        appended: bool,
        /// Delivery attempt of the primary call that follows: 1 at a
        /// turn's start, the next one after a forced summary.
        attempt: u32,
    },
    /// `Cleared`: append the micro-compaction projection unless
    /// `appended`, then dispatch the turn's primary call `attempt`.
    Cleared {
        /// Index in the provider view where the kept turns start.
        before: u64,
        /// Delivery attempt of the primary call that follows.
        attempt: u32,
        /// Whether the projection is already in the session.
        appended: bool,
    },
    /// `CompactionPending` for a summary the provider's context-length
    /// refusal forced, and that settled without a summary: the prompt still
    /// does not fit, so the run ends failed.
    Unshortened,
}

/// How one call of a tool batch recovers.
#[derive(Debug, Clone, PartialEq)]
pub enum ToolRecovery {
    /// The call has a durable end: reuse its result.
    Done(ToolResultBlock),
    /// The call started but has no durable end: this error result stands in,
    /// and the tool is not run again. The run stages it as the call's end.
    Interrupted {
        /// Name of the tool that started.
        tool_name: String,
        /// The placeholder result.
        result: ToolResultBlock,
    },
    /// The call never started: plan it again, which asks for permission
    /// again unless a recorded decision covers it.
    Replan(ToolUseBlock),
}

/// The durable state does not fit the checkpoint.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ReconcileError {
    /// The session log length is neither the checkpoint's `session_len` nor
    /// one more with the pending message last.
    #[error("session log has {found} entries, the checkpoint expects {expected}")]
    SessionConflict {
        /// `session_len` from the checkpoint.
        expected: u64,
        /// Entries in the session log.
        found: u64,
    },
    /// `ProviderPending` with no delivery for its turn among the events.
    #[error("no model-call delivery for turn {turn} among the given events")]
    MissingDelivery {
        /// Turn of the checkpoint.
        turn: u32,
    },
    /// A tool phase whose session does not end with an assistant tool call.
    #[error("the session does not end with the batch's assistant tool calls")]
    MissingToolCalls,
}

/// Decide how a run resumes from its durable state. Pure: reads only its
/// arguments.
///
/// Args:
///     checkpoint: The run's committed checkpoint.
///     events: Durable events of the run. `ProviderPending` and
///         `CompactionPending` need the current turn's delivery among them,
///         and the compaction phases read the run's deliveries to tell a
///         forced summary from a turn-start one; `ToolBatch` reads only
///         events after its `after_seq`. Passing every event is always
///         enough.
///     session: Every entry of the run's session log.
///
/// Returns:
///     The recovery for the checkpoint's phase.
///
/// Raises:
///     ReconcileError: If the session or events do not fit the checkpoint.
pub fn reconcile(
    checkpoint: &Checkpoint,
    events: &[Envelope],
    session: &[LogEntry],
) -> Result<Recovery, ReconcileError> {
    match &checkpoint.phase {
        Phase::Started { prompt } => Ok(Recovery::Started {
            appended: prompt_appended(checkpoint, session, prompt)?,
            prompt: prompt.clone(),
        }),
        Phase::ProviderPending { attempt } => {
            provider_pending(checkpoint, *attempt, events, session)
        }
        Phase::AssistantReceived {
            message,
            stop_reason,
            retry_feedback,
        } => Ok(Recovery::Received {
            appended: appended(checkpoint, session, message)?,
            message: message.clone(),
            stop_reason: *stop_reason,
            retry_feedback: retry_feedback.clone(),
        }),
        Phase::ToolBatch {
            decisions,
            after_seq,
        } => {
            let batch: Vec<&Envelope> = events.iter().filter(|e| e.seq > *after_seq).collect();
            let calls = tool_calls(checkpoint, session)?
                .map(|call| recover_call(call, &batch))
                .collect();
            Ok(Recovery::ToolBatch {
                decisions: decisions.clone(),
                calls,
            })
        }
        Phase::AwaitingApproval { decisions, pending } => {
            let _calls = tool_calls(checkpoint, session)?;
            Ok(Recovery::AwaitingApproval {
                decisions: decisions.clone(),
                pending: pending.clone(),
            })
        }
        Phase::AwaitingInput { decisions, pending } => {
            let _calls = tool_calls(checkpoint, session)?;
            Ok(Recovery::AwaitingInput {
                decisions: decisions.clone(),
                pending: pending.clone(),
            })
        }
        Phase::ToolResults { message } => Ok(Recovery::ToolResults {
            appended: appended(checkpoint, session, message)?,
            message: message.clone(),
        }),
        Phase::CompactionPending { attempt } => {
            let (delivery, receipt) = delivery(checkpoint, ModelCallKind::Compaction, events)?;
            unchanged(checkpoint, session)?;
            let forced = Forced::of(events, checkpoint.turn);
            // A summary lands with its `Compacted` checkpoint, so a receipt
            // here settled a call that gave none: it is not bought again.
            // A turn-start summary lets the turn go on uncompacted; a forced
            // one leaves a prompt the provider already refused.
            if receipt {
                if forced.latest_forced {
                    return Ok(Recovery::Unshortened);
                }
                return Ok(Recovery::Redispatch {
                    attempt: 1,
                    abandoned: None,
                });
            }
            Ok(Recovery::Recompact {
                attempt: attempt + 1,
                abandoned: Some(abandoned(delivery)),
                redispatch: (forced.latest_forced).then_some(forced.primary_calls + 1),
            })
        }
        Phase::Compacted { summary, keep_from } => Ok(Recovery::Compacted {
            appended: projected(checkpoint, session, summary)?,
            summary: summary.clone(),
            keep_from: *keep_from,
            attempt: Forced::of(events, checkpoint.turn).primary_calls + 1,
        }),
        Phase::Cleared { before, attempt } => Ok(Recovery::Cleared {
            appended: cleared(checkpoint, session)?,
            before: *before,
            attempt: *attempt,
        }),
    }
}

fn provider_pending(
    checkpoint: &Checkpoint,
    attempt: u32,
    events: &[Envelope],
    session: &[LogEntry],
) -> Result<Recovery, ReconcileError> {
    let (delivery, receipt) = delivery(checkpoint, ModelCallKind::Primary, events)?;
    unchanged(checkpoint, session)?;
    // A response lands with its `AssistantReceived` checkpoint, so a receipt
    // here settled the call without one: it failed, was cancelled, or was
    // accepted and written before the hooks that follow the stream, which
    // never finished (an orphan). Each is paid and already counted in the
    // checkpoint's totals; the call goes again, and is not a retry.
    Ok(Recovery::Redispatch {
        attempt: attempt + 1,
        abandoned: (!receipt).then(|| abandoned(delivery)),
    })
}

/// The latest delivery of `kind` for the checkpoint's turn, and whether
/// a receipt settled it.
fn delivery<'a>(
    checkpoint: &Checkpoint,
    kind: ModelCallKind,
    events: &'a [Envelope],
) -> Result<(&'a ModelCallDeliveryEvent, bool), ReconcileError> {
    let turn = checkpoint.turn;
    let (at, delivery) = (events.iter().enumerate().rev())
        .find_map(|(at, envelope)| match &envelope.event {
            Event::ModelCallDelivery(d)
                if d.kind == kind && d.turn_index == Some(u64::from(turn)) =>
            {
                Some((at, d))
            }
            _ => None,
        })
        .ok_or(ReconcileError::MissingDelivery { turn })?;
    let receipt = events[at + 1..].iter().any(|envelope| {
        matches!(&envelope.event, Event::ModelCallReceipt(r)
            if r.delivery_attempt_id == delivery.delivery_attempt_id)
    });
    Ok((delivery, receipt))
}

/// A cancelled receipt that settles a delivery with no outcome.
fn abandoned(delivery: &ModelCallDeliveryEvent) -> ModelCallReceiptEvent {
    ModelCallReceiptEvent {
        run_id: delivery.run_id.clone(),
        logical_call_id: delivery.logical_call_id.clone(),
        delivery_attempt_id: delivery.delivery_attempt_id.clone(),
        kind: delivery.kind,
        actual_model: None,
        disposition: CallDisposition::Cancelled,
        usage: None,
        cost_usd: None,
        duration_ms: 0,
    }
}

fn recover_call(call: &ToolUseBlock, batch: &[&Envelope]) -> ToolRecovery {
    let mut started = false;
    for envelope in batch {
        match &envelope.event {
            Event::ToolCallStart(start) if start.tool_use_id == call.id => {
                started |= start.tool_name == call.name;
            }
            Event::ToolCallEnd(end)
                if started && end.tool_use_id() == call.id && end.tool_name == call.name =>
            {
                return ToolRecovery::Done(end.result.clone());
            }
            _ => {}
        }
    }
    if started {
        ToolRecovery::Interrupted {
            tool_name: call.name.clone(),
            result: ToolResultBlock::text(&call.id, INTERRUPTED_TOOL_TEXT, true),
        }
    } else {
        ToolRecovery::Replan(call.clone())
    }
}

/// The tool calls of the assistant message that ends the session.
fn tool_calls<'a>(
    checkpoint: &Checkpoint,
    session: &'a [LogEntry],
) -> Result<impl Iterator<Item = &'a ToolUseBlock>, ReconcileError> {
    unchanged(checkpoint, session)?;
    match session.last() {
        Some(LogEntry::Message(entry))
            if entry.message.role == Role::Assistant
                && entry.message.tool_uses().next().is_some() =>
        {
            Ok(entry.message.tool_uses())
        }
        _ => Err(ReconcileError::MissingToolCalls),
    }
}
