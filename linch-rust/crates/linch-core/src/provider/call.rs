//! One primary model call: its delivery record, stream assembly and receipt.
//!
//! The loop runs a call in two steps around the journal barrier.
//! [`ModelCall::prepare`] builds the [`ModelCallDeliveryEvent`], which must
//! be durable before dispatch. [`ModelCall::drive`] then streams the response
//! through [`Assembler`], forwards stream-only deltas, and settles the call
//! with exactly one [`ModelCallReceiptEvent`]. `drive` takes the call by
//! value, so one delivery cannot be settled twice.

use std::time::Instant;

use futures::StreamExt;
use tokio_util::sync::CancellationToken;

use super::{
    Assembler, AssistantAssembly, Provider, ProviderError, ProviderEvent, ProviderRequest,
};
use crate::events::kinds::{ErrorEvent, PartialAssistantEvent};
use crate::events::model_call::{
    CallDisposition, ModelCallDeliveryEvent, ModelCallKind, ModelCallReceiptEvent,
};
use crate::ids::RunId;
use crate::json::{JsonObject, JsonValue};
use crate::provider::ProviderStream;
use crate::usage::Usage;

#[cfg(test)]
mod tests;

/// Receives each stream-only delta as soon as the provider sends it.
pub type DeltaSink<'a> = dyn FnMut(PartialAssistantEvent) + Send + 'a;

/// A prepared primary call: the delivery record, waiting to be driven.
///
/// Deliberately not `Clone`: [`ModelCall::drive`] consumes it, so each
/// delivery settles once. Clone the [`ModelCall::delivery`] record instead.
#[derive(Debug, PartialEq)]
pub struct ModelCall {
    delivery: ModelCallDeliveryEvent,
}

/// The settled result of [`ModelCall::drive`].
///
/// `receipt` is present on every path. Its disposition is `accepted` when
/// `result` is `Ok`, `cancelled` for [`ProviderError::Cancelled`], and
/// `failed` otherwise. Its usage is the usage the provider reported, even on
/// a failed call, and `None` when the provider never reported any.
#[derive(Debug)]
pub struct CallOutcome {
    /// The one receipt for this delivery.
    pub receipt: ModelCallReceiptEvent,
    /// The assembled response, or why the call failed.
    pub result: Result<AssistantAssembly, CallFailure>,
}

/// A failed call: the provider error and the [`ErrorEvent`] payload for it.
#[derive(Debug)]
pub struct CallFailure {
    /// What went wrong.
    pub error: ProviderError,
    /// The error as an event payload: `name`, `kind`, `message`,
    /// `retryable`, and `status` for an HTTP status error.
    pub event: ErrorEvent,
}

/// What the stream revealed before it ended, kept for the receipt.
#[derive(Default)]
struct Observed {
    model: Option<String>,
    usage: Option<Usage>,
    /// Whether a `message_end` arrived, with or without usage.
    ended: bool,
}

impl ModelCall {
    /// Prepare attempt `attempt` (from 1) of the primary call for `turn`.
    ///
    /// Ids are derived from the arguments only, so a resumed run rebuilds
    /// the same delivery from its checkpoint: the logical call id is
    /// `{run_id}:primary:{turn}` and the delivery attempt id appends
    /// `:{attempt}`.
    ///
    /// Args:
    ///     run_id: Run making the call.
    ///     turn: Turn index, from 0.
    ///     attempt: Delivery attempt for this turn, from 1.
    ///     model: Model the request asks for.
    pub fn prepare(run_id: &RunId, turn: u32, attempt: u32, model: &str) -> Self {
        let logical_call_id = format!("{run_id}:primary:{turn}");
        let delivery_attempt_id = format!("{logical_call_id}:{attempt}");
        Self {
            delivery: ModelCallDeliveryEvent {
                run_id: run_id.clone(),
                logical_call_id,
                delivery_attempt_id,
                kind: ModelCallKind::Primary,
                turn_index: Some(u64::from(turn)),
                requested_model: model.to_owned(),
            },
        }
    }

    /// The delivery record to stage before dispatch.
    pub fn delivery(&self) -> &ModelCallDeliveryEvent {
        &self.delivery
    }

    /// Dispatch the call and settle it.
    ///
    /// A cancelled `cancel` ends the call even if the provider ignores the
    /// token. After a stream contract violation the rest of the stream is
    /// still read for its `message_end`, so the spend the provider reports
    /// reaches the receipt.
    ///
    /// Args:
    ///     provider: Provider to call.
    ///     request: Request to send; its model should match the delivery's.
    ///     cancel: Aborts the call.
    ///     on_delta: Receives text, thinking and tool-input deltas, after
    ///         each one passes the stream contract; `None` skips building
    ///         them.
    ///
    /// Returns:
    ///     The receipt and the assembled response or failure.
    pub async fn drive(
        self,
        provider: &dyn Provider,
        request: ProviderRequest,
        cancel: &CancellationToken,
        on_delta: Option<&mut DeltaSink<'_>>,
    ) -> CallOutcome {
        let started = Instant::now();
        let mut seen = Observed::default();
        let mut result = cancel
            .run_until_cancelled(consume(provider, request, cancel, on_delta, &mut seen))
            .await
            .unwrap_or(Err(ProviderError::Cancelled));
        if result.is_err() && cancel.is_cancelled() {
            // A provider may report an abort as some other error.
            result = Err(ProviderError::Cancelled);
        }
        let disposition = match &result {
            Ok(_) => CallDisposition::Accepted,
            Err(ProviderError::Cancelled) => CallDisposition::Cancelled,
            Err(_) => CallDisposition::Failed,
        };
        let ModelCallDeliveryEvent {
            run_id,
            logical_call_id,
            delivery_attempt_id,
            kind,
            ..
        } = self.delivery;
        let receipt = ModelCallReceiptEvent {
            run_id,
            logical_call_id,
            delivery_attempt_id,
            kind,
            actual_model: seen.model,
            disposition,
            usage: seen.usage,
            cost_usd: None,
            duration_ms: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
        };
        let result = result.map_err(|error| CallFailure {
            event: error_event(&error),
            error,
        });
        CallOutcome { receipt, result }
    }
}

async fn consume(
    provider: &dyn Provider,
    request: ProviderRequest,
    cancel: &CancellationToken,
    mut on_delta: Option<&mut DeltaSink<'_>>,
    seen: &mut Observed,
) -> Result<AssistantAssembly, ProviderError> {
    let mut stream = provider.stream(request, cancel.clone()).await?;
    let mut assembler = Assembler::new();
    while let Some(item) = stream.next().await {
        // A sink may cancel; the provider may not notice before its next item.
        if cancel.is_cancelled() {
            return Err(ProviderError::Cancelled);
        }
        let event = item?;
        let delta = on_delta.as_ref().and_then(|_| partial(&event));
        let model = match &event {
            ProviderEvent::MessageStart(start) => Some(start.model.clone()),
            ProviderEvent::MessageEnd(end) => {
                seen.ended = true;
                if seen.usage.is_none() {
                    seen.usage = end.usage;
                }
                None
            }
            _ => None,
        };
        if let Err(error) = assembler.push(event) {
            if !seen.ended {
                drain_usage(&mut stream, cancel, seen).await;
            }
            return Err(error.into());
        }
        if model.is_some() {
            seen.model = model;
        }
        if let (Some(sink), Some(delta)) = (on_delta.as_deref_mut(), delta) {
            sink(delta);
        }
    }
    Ok(assembler.finish()?)
}

/// Read the rest of a broken stream for the usage in its `message_end`.
async fn drain_usage(stream: &mut ProviderStream, cancel: &CancellationToken, seen: &mut Observed) {
    while let Some(Ok(event)) = stream.next().await {
        if cancel.is_cancelled() {
            return;
        }
        if let ProviderEvent::MessageEnd(end) = event {
            seen.usage = end.usage;
            return;
        }
    }
}

/// The stream-only delta for a content event, in the Python wire shape.
fn partial(event: &ProviderEvent) -> Option<PartialAssistantEvent> {
    let fields: Vec<(&str, &str)> = match event {
        ProviderEvent::TextDelta(delta) => vec![("kind", "text"), ("text", &delta.text)],
        ProviderEvent::ThinkingDelta(delta) => vec![("kind", "thinking"), ("text", &delta.text)],
        ProviderEvent::ToolUseInputDelta(delta) => vec![
            ("kind", "tool_use_input"),
            ("tool_use_id", &delta.id),
            ("json_delta", &delta.json_delta),
        ],
        _ => return None,
    };
    let delta = fields
        .into_iter()
        .map(|(key, value)| (key.to_owned(), JsonValue::from(value)))
        .collect();
    Some(PartialAssistantEvent { delta })
}

fn error_event(error: &ProviderError) -> ErrorEvent {
    let kind = match error {
        ProviderError::Transport { .. } => "transport",
        ProviderError::Status { .. } => "status",
        ProviderError::InvalidRequest { .. } => "invalid_request",
        ProviderError::Stream(_) => "stream",
        ProviderError::Cancelled => "cancelled",
    };
    let mut payload = JsonObject::new();
    payload.insert("name".into(), "ProviderError".into());
    payload.insert("kind".into(), kind.into());
    payload.insert("message".into(), error.to_string().into());
    payload.insert("retryable".into(), error.retryable().into());
    if let ProviderError::Status { status, .. } = error {
        payload.insert("status".into(), (*status).into());
    }
    ErrorEvent { error: payload }
}
