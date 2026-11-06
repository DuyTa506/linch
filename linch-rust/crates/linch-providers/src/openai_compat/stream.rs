//! Chat Completions SSE chunks to normalized [`ProviderEvent`]s.
//!
//! [`Translator`] is pure: it turns one `data:` payload at a time into
//! events. [`events`] drives it over a byte stream with cancellation.

use std::collections::{HashMap, VecDeque};

use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{JsonValue, ProviderError, ProviderEvent, StopReason, Usage};

use super::http;
use crate::sse::Decoder;
pub(crate) use crate::sse::events;

mod wire;

use wire::{Chunk, Delta};
pub(crate) use wire::{ReasoningField, WireUsage};

/// A tool call seen on the stream, in first-seen order.
#[derive(Default)]
struct Call {
    id: String,
    name: String,
    started: bool,
    has_arguments: bool,
}

/// Turns chunk payloads into events for one response.
pub(crate) struct Translator {
    fallback_model: String,
    reasoning_field: Option<ReasoningField>,
    usage_of: fn(&WireUsage) -> Usage,
    started: bool,
    calls: Vec<Call>,
    by_index: HashMap<u32, usize>,
    finish: Option<StopReason>,
    usage: Option<Usage>,
}

impl Translator {
    /// `fallback_model` names the model when no chunk reports one;
    /// `reasoning_field` is the delta field read as reasoning, if any.
    pub(crate) fn new(
        fallback_model: String,
        reasoning_field: Option<ReasoningField>,
        usage_of: fn(&WireUsage) -> Usage,
    ) -> Self {
        Self {
            fallback_model,
            reasoning_field,
            usage_of,
            started: false,
            calls: Vec::new(),
            by_index: HashMap::new(),
            finish: None,
            usage: None,
        }
    }

    /// Translate one `data:` payload other than `[DONE]`.
    pub(crate) fn push(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let chunk: Chunk = serde_json::from_str(data)
            .map_err(|err| transport(&format!("malformed stream chunk: {err}")))?;
        if let Some(error) = chunk.error.filter(|error| !error.is_null()) {
            return Err(stream_error(&error));
        }
        if !self.started {
            self.started = true;
            let model = chunk
                .model
                .filter(|model| !model.trim().is_empty())
                .unwrap_or_else(|| self.fallback_model.clone());
            out.push_back(ProviderEvent::MessageStart(MessageStartEvent { model }));
        }
        // Usage may ride on the last content chunk or on a chunk of its own
        // with no choices; the last one seen wins.
        if let Some(usage) = &chunk.usage {
            self.usage = Some((self.usage_of)(usage));
        }
        let Some(choice) = chunk.choices.and_then(|choices| choices.into_iter().next()) else {
            return Ok(());
        };
        if let Some(delta) = choice.delta {
            self.push_delta(delta, out);
        }
        if let Some(reason) = choice.finish_reason {
            self.finish = Some(stop_reason(&reason)?);
        }
        Ok(())
    }

    fn push_delta(&mut self, delta: Delta, out: &mut VecDeque<ProviderEvent>) {
        let reasoning = match self.reasoning_field {
            Some(ReasoningField::ReasoningContent) => delta.reasoning_content,
            Some(ReasoningField::Reasoning) => delta.reasoning,
            None => None,
        };
        if let Some(text) = reasoning.filter(|text| !text.is_empty()) {
            out.push_back(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
                text,
                signature: None,
            }));
        }
        if let Some(text) = delta.content.filter(|text| !text.is_empty()) {
            out.push_back(ProviderEvent::TextDelta(TextDeltaEvent { text }));
        }
        for piece in delta.tool_calls.unwrap_or_default() {
            let (name, arguments) = piece
                .function
                .map(|function| (function.name, function.arguments))
                .unwrap_or_default();
            let slot = *self.by_index.entry(piece.index).or_insert_with(|| {
                self.calls.push(Call::default());
                self.calls.len() - 1
            });
            let call = &mut self.calls[slot];
            // A call opens at its first argument piece. Until then a later
            // chunk may complete its id or name: llama.cpp re-sends a name
            // that grew while it was parsed. An id that stays empty is left
            // for the assembler to reject.
            if !call.started {
                if let Some(id) = piece.id.filter(|id| !id.is_empty()) {
                    call.id = id;
                }
                if let Some(name) = name.filter(|name| !name.is_empty()) {
                    call.name = name;
                }
            }
            if let Some(json_delta) = arguments.filter(|arguments| !arguments.is_empty()) {
                open(call, out);
                call.has_arguments = true;
                out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                    id: call.id.clone(),
                    json_delta,
                }));
            }
        }
    }

    /// Close the response at `[DONE]`: end every call, then the message.
    ///
    /// Any tool call makes the stop reason `tool_use`, even when the server
    /// said `stop`. A call with no arguments gets `{}`.
    pub(crate) fn finish(
        &mut self,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        if !self.started {
            return Err(transport("stream ended at [DONE] before any chunk"));
        }
        let Some(finish) = self.finish else {
            return Err(transport("stream ended at [DONE] without a finish_reason"));
        };
        let stop_reason = if self.calls.is_empty() {
            finish
        } else {
            StopReason::ToolUse
        };
        for mut call in std::mem::take(&mut self.calls) {
            open(&mut call, out);
            if !call.has_arguments {
                out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                    id: call.id.clone(),
                    json_delta: "{}".into(),
                }));
            }
            out.push_back(ProviderEvent::ToolUseEnd(ToolUseEndEvent { id: call.id }));
        }
        out.push_back(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason,
            // `include_usage` is always requested; if no chunk carried usage
            // it stays unknown (`None`), never a zero fill.
            usage: self.usage,
            provider_metadata: None,
        }));
        Ok(())
    }
}

fn open(call: &mut Call, out: &mut VecDeque<ProviderEvent>) {
    if !call.started {
        call.started = true;
        out.push_back(ProviderEvent::ToolUseStart(ToolUseStartEvent {
            id: call.id.clone(),
            name: call.name.clone(),
        }));
    }
}

/// `finish_reason` to a stop reason. A reason that is not a normal stop
/// (`insufficient_system_resource`, `aborted`, `abort`, anything new)
/// fails the call as a retryable transport error.
fn stop_reason(reason: &str) -> Result<StopReason, ProviderError> {
    match reason {
        "stop" => Ok(StopReason::EndTurn),
        "tool_calls" => Ok(StopReason::ToolUse),
        "length" => Ok(StopReason::MaxTokens),
        "content_filter" => Ok(StopReason::Refusal),
        other => Err(transport(&format!(
            "response ended with finish_reason {other:?}"
        ))),
    }
}

/// An error frame (`{"error": {...}}`) sent after the stream started. Its
/// `code` or `type` gives the HTTP status the same error has outside a
/// stream (see [`http::stream_status`]), so retrying follows the server's
/// own classification; an unknown error is a retryable transport error.
fn stream_error(error: &JsonValue) -> ProviderError {
    let text = |key: &str| error.get(key).and_then(JsonValue::as_str);
    let detail = text("message")
        .or(error.as_str())
        .unwrap_or("unknown error");
    let message = match text("type") {
        Some(kind) => format!("stream error: {kind}: {detail}"),
        None => format!("stream error: {detail}"),
    };
    match http::stream_status(error.get("code"), text("type")) {
        Some(status) => ProviderError::Status {
            status,
            retry_after: None,
            message,
        },
        None => transport(&message),
    }
}

fn transport(message: &str) -> ProviderError {
    ProviderError::Transport {
        message: message.into(),
    }
}

impl Decoder for Translator {
    fn data(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError> {
        let data = data.trim();
        if data == "[DONE]" {
            self.finish(out)?;
            return Ok(true);
        }
        self.push(data, out)?;
        Ok(false)
    }

    fn close(&mut self, _out: &mut VecDeque<ProviderEvent>) -> Result<(), ProviderError> {
        Err(transport("stream closed before [DONE]"))
    }
}

#[cfg(test)]
mod tests;
