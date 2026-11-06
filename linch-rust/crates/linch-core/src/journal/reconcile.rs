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

use session::{appended, projected, prompt_appended, unchanged};

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
    /// `ProviderPending` without an accepted receipt: write `abandoned`, if
    /// any, then dispatch `attempt`.
    Redispatch {
        /// The new delivery attempt number.
        attempt: u32,
        /// A cancelled receipt for the unsettled delivery; `None` when the
        /// delivery already has a failed or cancelled receipt.
        abandoned: Option<ModelCallReceiptEvent>,
    },
    /// A response is durable (`AssistantReceived`, or `ProviderPending` with
    /// an accepted receipt): append it unless `appended`, then go on.
    Received {
        /// The assistant message.
        message: Message,
        /// Why the response stopped.
        stop_reason: StopReason,
        /// Whether the message is already in the session.
        appended: bool,
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
    /// `ToolResults`: append the message unless `appended`, then go on.
    ToolResults {
        /// The user message to append (tool results or stop feedback).
        message: Message,
        /// Whether the message is already in the session.
        appended: bool,
    },
    /// `CompactionPending` without a receipt, or with a failed or
    /// cancelled one: write `abandoned`, if any, then dispatch summary
    /// `attempt`.
    Recompact {
        /// The new delivery attempt number.
        attempt: u32,
        /// A cancelled receipt for the unsettled delivery; `None` when the
        /// delivery already has a receipt.
        abandoned: Option<ModelCallReceiptEvent>,
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
    },
}

/// How one call of a tool batch recovers.
#[derive(Debug, Clone, PartialEq)]
pub enum ToolRecovery {
    /// The call has a durable end: reuse its result.
    Done(ToolResultBlock),
    /// The call started but has no durable end: this error result stands in,
    /// and the tool is not run again.
    Interrupted(ToolResultBlock),
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
    /// An accepted receipt with no `assistant` event after it.
    #[error("accepted receipt {delivery_attempt_id} has no assistant event")]
    MissingAssistant {
        /// Delivery attempt of the receipt.
        delivery_attempt_id: String,
    },
    /// A tool phase whose session does not end with an assistant tool call.
    #[error("the session does not end with the batch's assistant tool calls")]
    MissingToolCalls,
    /// `CompactionPending` with an accepted receipt: the summary should have
    /// landed with it in a `Compacted` checkpoint, so the store lost a write.
    #[error("accepted compaction receipt {delivery_attempt_id} has no summary")]
    MissingSummary {
        /// Delivery attempt of the receipt.
        delivery_attempt_id: String,
    },
}

/// Decide how a run resumes from its durable state. Pure: reads only its
/// arguments.
///
/// Args:
///     checkpoint: The run's committed checkpoint.
///     events: Durable events of the run. `ProviderPending` and
///         `CompactionPending` need the current turn's delivery among them;
///         `ToolBatch` reads only events after its `after_seq`. Passing
///         every event is always enough.
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
        } => Ok(Recovery::Received {
            appended: appended(checkpoint, session, message)?,
            message: message.clone(),
            stop_reason: *stop_reason,
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
        Phase::ToolResults { message } => Ok(Recovery::ToolResults {
            appended: appended(checkpoint, session, message)?,
            message: message.clone(),
        }),
        Phase::CompactionPending { attempt } => {
            let (delivery, later, receipt) =
                delivery(checkpoint, ModelCallKind::Compaction, events)?;
            if let Some(i) = receipt
                && matches!(&later[i].event, Event::ModelCallReceipt(r)
                    if r.disposition == CallDisposition::Accepted)
            {
                return Err(ReconcileError::MissingSummary {
                    delivery_attempt_id: delivery.delivery_attempt_id.clone(),
                });
            }
            unchanged(checkpoint, session)?;
            Ok(Recovery::Recompact {
                attempt: attempt + 1,
                abandoned: receipt.is_none().then(|| abandoned(delivery)),
            })
        }
        Phase::Compacted { summary, keep_from } => Ok(Recovery::Compacted {
            appended: projected(checkpoint, session, summary)?,
            summary: summary.clone(),
            keep_from: *keep_from,
        }),
    }
}

fn provider_pending(
    checkpoint: &Checkpoint,
    attempt: u32,
    events: &[Envelope],
    session: &[LogEntry],
) -> Result<Recovery, ReconcileError> {
    let (delivery, later, receipt) = delivery(checkpoint, ModelCallKind::Primary, events)?;
    let accepted = receipt.filter(|&i| {
        matches!(&later[i].event, Event::ModelCallReceipt(r)
            if r.disposition == CallDisposition::Accepted)
    });
    let Some(accepted) = accepted else {
        unchanged(checkpoint, session)?;
        return Ok(Recovery::Redispatch {
            attempt: attempt + 1,
            abandoned: receipt.is_none().then(|| abandoned(delivery)),
        });
    };
    let response = later[accepted + 1..]
        .iter()
        .find_map(|envelope| match &envelope.event {
            Event::Assistant(response) => Some(response),
            _ => None,
        });
    let response = response.ok_or_else(|| ReconcileError::MissingAssistant {
        delivery_attempt_id: delivery.delivery_attempt_id.clone(),
    })?;
    Ok(Recovery::Received {
        appended: appended(checkpoint, session, &response.message)?,
        message: response.message.clone(),
        stop_reason: response.stop_reason,
    })
}

/// The latest delivery of `kind` for the checkpoint's turn, the events
/// after it, and the position of its receipt among them.
fn delivery<'a>(
    checkpoint: &Checkpoint,
    kind: ModelCallKind,
    events: &'a [Envelope],
) -> Result<(&'a ModelCallDeliveryEvent, &'a [Envelope], Option<usize>), ReconcileError> {
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
    let later = &events[at + 1..];
    let receipt = later.iter().position(|envelope| {
        matches!(&envelope.event, Event::ModelCallReceipt(r)
            if r.delivery_attempt_id == delivery.delivery_attempt_id)
    });
    Ok((delivery, later, receipt))
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
        ToolRecovery::Interrupted(ToolResultBlock::text(&call.id, INTERRUPTED_TOOL_TEXT, true))
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
