//! Chat Completions SSE chunks to normalized [`ProviderEvent`]s.
//!
//! [`Translator`] is pure: it turns one `data:` payload at a time into
//! events. [`events`] drives it over a byte stream with cancellation.

use std::collections::{HashMap, VecDeque};
use std::fmt::Display;

use eventsource_stream::Eventsource;
use futures::{Stream, StreamExt};
use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{ProviderError, ProviderEvent, ProviderStream, StopReason, Usage};
use serde::Deserialize;
use tokio_util::sync::CancellationToken;

use crate::ApiKey;

// Vendor chunks are read leniently: unknown fields (`system_fingerprint`,
// `logprobs`, new usage buckets) are ignored, and nulls read as absent.

#[derive(Deserialize)]
struct Chunk {
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    choices: Option<Vec<Choice>>,
    #[serde(default)]
    usage: Option<WireUsage>,
}

#[derive(Deserialize)]
struct Choice {
    #[serde(default)]
    delta: Option<Delta>,
    #[serde(default)]
    finish_reason: Option<String>,
}

#[derive(Deserialize)]
struct Delta {
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    reasoning_content: Option<String>,
    #[serde(default)]
    tool_calls: Option<Vec<ToolCallDelta>>,
}

#[derive(Deserialize)]
struct ToolCallDelta {
    index: u32,
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    function: Option<FunctionDelta>,
}

#[derive(Deserialize)]
struct FunctionDelta {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    arguments: Option<String>,
}

/// The `usage` object of a chunk, for an adapter's usage normalizer.
#[derive(Debug, Default, Deserialize)]
pub(crate) struct WireUsage {
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub completion_tokens: u64,
    #[serde(default)]
    pub prompt_tokens_details: Option<PromptTokensDetails>,
    /// DeepSeek's cache-hit count; `prompt_tokens` = hit + miss.
    #[serde(default)]
    pub prompt_cache_hit_tokens: Option<u64>,
}

/// `usage.prompt_tokens_details`.
#[derive(Debug, Default, Deserialize)]
pub(crate) struct PromptTokensDetails {
    #[serde(default)]
    pub cached_tokens: Option<u64>,
}

/// A tool call seen on the stream, in first-seen order.
struct Call {
    id: String,
    has_arguments: bool,
}

/// Turns chunk payloads into events for one response.
pub(crate) struct Translator {
    fallback_model: String,
    usage_of: fn(&WireUsage) -> Usage,
    started: bool,
    calls: Vec<Call>,
    by_index: HashMap<u32, usize>,
    finish: Option<StopReason>,
    usage: Option<Usage>,
}

impl Translator {
    /// `fallback_model` names the model when no chunk reports one.
    pub(crate) fn new(fallback_model: String, usage_of: fn(&WireUsage) -> Usage) -> Self {
        Self {
            fallback_model,
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
        let chunk: Chunk = serde_json::from_str(data).map_err(|err| ProviderError::Transport {
            message: format!("malformed stream chunk: {err}"),
        })?;
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
        if let Some(text) = delta.reasoning_content.filter(|text| !text.is_empty()) {
            out.push_back(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
                text,
                signature: None,
            }));
        }
        if let Some(text) = delta.content.filter(|text| !text.is_empty()) {
            out.push_back(ProviderEvent::TextDelta(TextDeltaEvent { text }));
        }
        for call in delta.tool_calls.unwrap_or_default() {
            let (name, arguments) = call
                .function
                .map(|function| (function.name, function.arguments))
                .unwrap_or_default();
            let slot = match self.by_index.get(&call.index) {
                Some(&slot) => slot,
                None => {
                    // Only a call's first chunk carries its id and name; an
                    // empty one is left for the assembler to reject.
                    let id = call.id.unwrap_or_default();
                    out.push_back(ProviderEvent::ToolUseStart(ToolUseStartEvent {
                        id: id.clone(),
                        name: name.unwrap_or_default(),
                    }));
                    self.by_index.insert(call.index, self.calls.len());
                    self.calls.push(Call {
                        id,
                        has_arguments: false,
                    });
                    self.calls.len() - 1
                }
            };
            if let Some(json_delta) = arguments.filter(|arguments| !arguments.is_empty()) {
                let call = &mut self.calls[slot];
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
        for call in std::mem::take(&mut self.calls) {
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
            // `include_usage` is always requested; `MessageEndEvent` has no
            // "unknown" state, so a missing usage chunk reads as zero.
            usage: self.usage.unwrap_or_default(),
            provider_metadata: None,
        }));
        Ok(())
    }
}

/// `finish_reason` to a stop reason. A reason that is not a normal stop
/// (`insufficient_system_resource`, `aborted`, anything new) fails the
/// call as a retryable transport error.
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

fn transport(message: &str) -> ProviderError {
    ProviderError::Transport {
        message: message.into(),
    }
}

struct State<S> {
    sse: S,
    translator: Translator,
    pending: VecDeque<ProviderEvent>,
    cancel: CancellationToken,
    key: ApiKey,
    done: bool,
}

/// Drive `translator` over an SSE byte stream until `[DONE]`, an error or
/// `cancel`. Transport error text is redacted with `key`.
pub(crate) fn events<S, B, E>(
    bytes: S,
    translator: Translator,
    cancel: CancellationToken,
    key: ApiKey,
) -> ProviderStream
where
    S: Stream<Item = Result<B, E>> + Send + 'static,
    B: AsRef<[u8]> + Send + 'static,
    E: Display + Send + 'static,
{
    let state = State {
        sse: Box::pin(bytes.eventsource()),
        translator,
        pending: VecDeque::new(),
        cancel,
        key,
        done: false,
    };
    futures::stream::unfold(state, |mut state| async move {
        let item = next(&mut state).await?;
        Some((item, state))
    })
    .boxed()
}

async fn next<S, E>(state: &mut State<S>) -> Option<Result<ProviderEvent, ProviderError>>
where
    S: Stream<Item = Result<eventsource_stream::Event, eventsource_stream::EventStreamError<E>>>
        + Unpin,
    E: Display,
{
    loop {
        if state.done && state.pending.is_empty() {
            return None;
        }
        if state.cancel.is_cancelled() {
            return Some(Err(stop(state, ProviderError::Cancelled)));
        }
        if let Some(event) = state.pending.pop_front() {
            return Some(Ok(event));
        }
        let failure = match state.cancel.run_until_cancelled(state.sse.next()).await {
            None => ProviderError::Cancelled,
            Some(None) => transport("stream closed before [DONE]"),
            Some(Some(Err(err))) => transport(&state.key.redact(&err.to_string())),
            Some(Some(Ok(event))) => match on_data(state, &event.data) {
                Ok(()) => continue,
                Err(err) => err,
            },
        };
        return Some(Err(stop(state, failure)));
    }
}

fn on_data<S>(state: &mut State<S>, data: &str) -> Result<(), ProviderError> {
    match data.trim() {
        "" => Ok(()),
        "[DONE]" => {
            state.done = true;
            state.translator.finish(&mut state.pending)
        }
        data => state.translator.push(data, &mut state.pending),
    }
}

/// End the stream after `err`: nothing is yielded after an error.
fn stop<S>(state: &mut State<S>, err: ProviderError) -> ProviderError {
    state.done = true;
    state.pending.clear();
    err
}

#[cfg(test)]
mod tests;
