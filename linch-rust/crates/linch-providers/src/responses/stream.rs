//! Responses API stream events to normalized [`ProviderEvent`]s.
//!
//! [`Translator`] is pure: it turns one `data:` payload at a time into
//! events. [`events`] drives it over a byte stream with cancellation. The
//! stream has no `[DONE]` sentinel; it ends at the terminal
//! `response.completed`, `response.incomplete` or `response.failed` event.

use std::collections::{HashMap, VecDeque};

use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{JsonValue, ProviderError, ProviderEvent, StopReason};
use serde_json::json;

use crate::openai_compat::http;
use crate::sse::Decoder;
pub(crate) use crate::sse::events;

mod wire;

use wire::{Item, WireError, WireEvent, WireResponse, usage};

/// The `provider_metadata` key the response id and output items live under.
pub(crate) const METADATA_KEY: &str = "openai_responses";

/// An open function call, keyed by its output item id.
struct Call {
    call_id: String,
    has_arguments: bool,
}

/// Turns event payloads into events for one response.
pub(crate) struct Translator {
    fallback_model: String,
    started: bool,
    calls: HashMap<String, Call>,
    completed_calls: usize,
    refused: bool,
    /// The reasoning part the last thinking delta belonged to, so a new
    /// part starts on a new paragraph.
    reasoning_part: Option<(String, u32)>,
}

impl Translator {
    /// `fallback_model` names the model when no event reports one.
    pub(crate) fn new(fallback_model: String) -> Self {
        Self {
            fallback_model,
            started: false,
            calls: HashMap::new(),
            completed_calls: 0,
            refused: false,
            reasoning_part: None,
        }
    }

    /// Translate one `data:` payload. Returns `true` after the terminal
    /// event, which closes the message.
    pub(crate) fn push(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError> {
        let event: WireEvent = serde_json::from_str(data)
            .map_err(|err| transport(format!("malformed stream event: {err}")))?;
        if !self.started {
            self.started = true;
            let model = event
                .response
                .as_ref()
                .and_then(|response| response.model.clone())
                .filter(|model| !model.trim().is_empty())
                .unwrap_or_else(|| self.fallback_model.clone());
            out.push_back(ProviderEvent::MessageStart(MessageStartEvent { model }));
        }
        let delta = event.delta.filter(|delta| !delta.is_empty());
        match event.kind.as_str() {
            "response.output_text.delta" => text(delta, out),
            "response.refusal.delta" => {
                self.refused = true;
                text(delta, out);
            }
            "response.reasoning_summary_text.delta" | "response.reasoning_text.delta" => {
                let part = (
                    event.item_id.unwrap_or_default(),
                    event.summary_index.unwrap_or(0),
                );
                self.thinking(part, delta, out);
            }
            "response.output_item.added" => self.open_call(event.item, out)?,
            "response.function_call_arguments.delta" => {
                let call = event.item_id.and_then(|id| self.calls.get_mut(&id));
                if let (Some(call), Some(json_delta)) = (call, delta) {
                    call.has_arguments = true;
                    out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                        id: call.call_id.clone(),
                        json_delta,
                    }));
                }
            }
            "response.output_item.done" => self.close_call(event.item, out),
            "response.completed" | "response.incomplete" => {
                self.finish(event.response.unwrap_or_default(), out)?;
                return Ok(true);
            }
            "response.failed" => {
                let error = event.response.and_then(|response| response.error);
                return Err(failure("response failed", error));
            }
            "error" => {
                let error = event.error.unwrap_or(WireError {
                    kind: None,
                    code: event.code,
                    message: event.message,
                });
                return Err(failure("stream error", Some(error)));
            }
            _ => {}
        }
        Ok(false)
    }

    fn thinking(
        &mut self,
        part: (String, u32),
        delta: Option<String>,
        out: &mut VecDeque<ProviderEvent>,
    ) {
        let Some(mut text) = delta else { return };
        match &self.reasoning_part {
            Some(previous) if *previous != part => text.insert_str(0, "\n\n"),
            _ => {}
        }
        self.reasoning_part = Some(part);
        out.push_back(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text,
            signature: None,
        }));
    }

    fn open_call(
        &mut self,
        item: Option<Item>,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let Some(item) = item.filter(|item| item.kind == "function_call") else {
            return Ok(());
        };
        let (Some(item_id), Some(call_id)) = (item.id, item.call_id) else {
            return Err(transport("function_call item without id or call_id".into()));
        };
        out.push_back(ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: call_id.clone(),
            name: item.name.unwrap_or_default(),
        }));
        self.calls.insert(
            item_id,
            Call {
                call_id,
                has_arguments: false,
            },
        );
        Ok(())
    }

    /// A call with no argument deltas takes the item's full arguments, or
    /// `{}` when they are blank.
    fn close_call(&mut self, item: Option<Item>, out: &mut VecDeque<ProviderEvent>) {
        let Some(item) = item.filter(|item| item.kind == "function_call") else {
            return;
        };
        let Some(call) = item.id.and_then(|id| self.calls.remove(&id)) else {
            return;
        };
        if !call.has_arguments {
            let arguments = item.arguments.unwrap_or_default();
            let arguments = arguments.trim();
            out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                id: call.call_id.clone(),
                json_delta: if arguments.is_empty() {
                    "{}"
                } else {
                    arguments
                }
                .into(),
            }));
        }
        out.push_back(ProviderEvent::ToolUseEnd(ToolUseEndEvent {
            id: call.call_id,
        }));
        self.completed_calls += 1;
    }

    /// Close the message. A completed call makes the stop reason
    /// `tool_use`; a call still open is left for the assembler to reject.
    fn finish(
        &mut self,
        response: WireResponse,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let stop_reason = if self.completed_calls > 0 {
            StopReason::ToolUse
        } else if self.refused {
            StopReason::Refusal
        } else {
            let reason = response
                .incomplete_details
                .and_then(|details| details.reason);
            stop_reason(response.status.as_deref(), reason.as_deref())?
        };
        let provider_metadata = metadata(response.id, response.output);
        out.push_back(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason,
            usage: response.usage.map(usage),
            provider_metadata,
        }));
        Ok(())
    }
}

fn text(delta: Option<String>, out: &mut VecDeque<ProviderEvent>) {
    if let Some(text) = delta {
        out.push_back(ProviderEvent::TextDelta(TextDeltaEvent { text }));
    }
}

/// `status` and `incomplete_details.reason` to a stop reason.
fn stop_reason(status: Option<&str>, reason: Option<&str>) -> Result<StopReason, ProviderError> {
    match (status, reason) {
        (Some("completed"), _) => Ok(StopReason::EndTurn),
        (Some("incomplete"), Some("max_output_tokens")) => Ok(StopReason::MaxTokens),
        (Some("incomplete"), Some("content_filter")) => Ok(StopReason::Refusal),
        (status, reason) => Err(transport(format!(
            "response ended with status {status:?} and reason {reason:?}"
        ))),
    }
}

/// The response id and its output items, replayed verbatim on later turns
/// so reasoning items stay next to the calls and messages they led to.
fn metadata(id: Option<String>, output: Option<Vec<JsonValue>>) -> Option<linch_core::JsonObject> {
    let output = output.filter(|output| !output.is_empty());
    if id.is_none() && output.is_none() {
        return None;
    }
    let mut entry = serde_json::Map::new();
    if let Some(id) = id {
        entry.insert("response_id".into(), id.into());
    }
    if let Some(output) = output {
        entry.insert("output".into(), output.into());
    }
    json!({ METADATA_KEY: entry }).as_object().cloned()
}

/// A failed response or an error event. Its code or type gives the HTTP
/// status the same error has outside a stream, so it retries alike; an
/// unknown error is a retryable transport error.
fn failure(what: &str, error: Option<WireError>) -> ProviderError {
    let WireError {
        kind,
        code,
        message,
    } = error.unwrap_or_default();
    let status = http::stream_status(code.as_ref(), kind.as_deref());
    let code = match code {
        Some(JsonValue::String(code)) => code,
        Some(JsonValue::Null) | None => "unknown".into(),
        Some(other) => other.to_string(),
    };
    let message = format!("{what}: {code}: {}", message.unwrap_or_default().trim());
    match status {
        Some(status) => ProviderError::Status {
            status,
            retry_after: None,
            message,
        },
        None => transport(message),
    }
}

fn transport(message: String) -> ProviderError {
    ProviderError::Transport { message }
}

impl Decoder for Translator {
    fn data(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError> {
        // Not part of the Responses protocol, but tolerated like the SDK
        // does: it ends a stream that never finished.
        if data.trim() == "[DONE]" {
            return Err(transport(
                "stream ended at [DONE] before the response finished".into(),
            ));
        }
        self.push(data, out)
    }

    fn close(&mut self, _out: &mut VecDeque<ProviderEvent>) -> Result<(), ProviderError> {
        Err(transport(
            "stream closed before the response finished".into(),
        ))
    }
}

#[cfg(test)]
mod tests;
