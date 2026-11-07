//! The scripted provider for tests, evals and benchmarks.
//!
//! [`ScriptedProvider`] replays a fixed list of [`ScriptedTurn`]s, one per
//! [`Provider::stream`] call, and records every request it receives. A turn
//! is a plain [`ProviderEvent`] list plus an optional error, so a script is
//! JSON that another language can replay; see [`ScriptedFixture`] for the
//! shape. [`TurnBuilder`] writes the common turns (text, thinking, tool calls
//! with chunked input, usage) without spelling out every event.

use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use async_trait::async_trait;
use futures::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio_util::sync::CancellationToken;

use super::client::DEFAULT_CONTEXT_WINDOW;
use super::{
    MessageEndEvent, MessageStartEvent, Provider, ProviderError, ProviderEvent, ProviderRequest,
    ProviderStream, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent, ToolUseInputDeltaEvent,
    ToolUseStartEvent,
};
use crate::json::JsonValue;
use crate::run::StopReason;
use crate::usage::Usage;

#[cfg(test)]
mod tests;

/// A replayable provider script: one turn per provider call, in call order.
///
/// The JSON shape, with each event in the [`ProviderEvent`] wire format:
///
/// ```json
/// {"turns": [
///   {"events": [
///     {"type": "message_start", "model": "m"},
///     {"type": "text_delta", "text": "hi"},
///     {"type": "message_end", "stop_reason": "end_turn",
///      "usage": {"input_tokens": 3, "output_tokens": 1,
///                "cache_read_tokens": 0, "cache_creation_tokens": 0}}
///   ]},
///   {"error": {"type": "status", "status": 503, "message": "busy"}}
/// ]}
/// ```
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScriptedFixture {
    /// Turns in call order.
    pub turns: Vec<ScriptedTurn>,
}

/// One scripted provider call: stream `events` in order, then fail with
/// `error` when it is set.
///
/// An `error` with no `events` fails the call itself ([`Provider::stream`]
/// returns it); an `error` after events breaks the stream mid-response.
/// Events are not checked here, so a turn may script a stream that breaks
/// the provider contract.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScriptedTurn {
    /// Events to stream, in order.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub events: Vec<ProviderEvent>,
    /// Error that ends the call after the events.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ScriptedError>,
}

impl ScriptedTurn {
    /// Start a turn whose `message_start` names `model`.
    pub fn builder(model: impl Into<String>) -> TurnBuilder {
        TurnBuilder::new(model)
    }

    /// A turn whose call fails with `error` before any event.
    pub fn failing(error: ScriptedError) -> Self {
        Self {
            events: Vec::new(),
            error: Some(error),
        }
    }
}

/// A [`ProviderError`] a script can carry. Stream contract violations are
/// scripted as a malformed event list, and cancellation comes from the token.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ScriptedError {
    /// [`ProviderError::Transport`].
    Transport {
        /// Error message.
        message: String,
    },
    /// [`ProviderError::Status`].
    Status {
        /// HTTP status code.
        status: u16,
        /// Server-requested retry delay in milliseconds.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        retry_after_ms: Option<u64>,
        /// Error message.
        message: String,
    },
    /// [`ProviderError::InvalidRequest`].
    InvalidRequest {
        /// Error message.
        message: String,
    },
}

impl From<ScriptedError> for ProviderError {
    fn from(error: ScriptedError) -> Self {
        match error {
            ScriptedError::Transport { message } => Self::Transport { message },
            ScriptedError::Status {
                status,
                retry_after_ms,
                message,
            } => Self::Status {
                status,
                retry_after: retry_after_ms.map(Duration::from_millis),
                message,
            },
            ScriptedError::InvalidRequest { message } => Self::InvalidRequest { message },
        }
    }
}

/// The script had no turn left for a call.
///
/// [`Provider::stream`] reports it as [`ProviderError::InvalidRequest`], the
/// non-retryable adapter-side variant, so a retrying caller does not spin.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
#[error("scripted provider ran out of turns after {consumed}")]
pub struct OutOfTurns {
    /// Turns consumed before the failing call.
    pub consumed: usize,
}

impl From<OutOfTurns> for ProviderError {
    fn from(error: OutOfTurns) -> Self {
        Self::InvalidRequest {
            message: error.to_string(),
        }
    }
}

/// A [`Provider`] that replays scripted turns and records every request.
///
/// Each [`Provider::stream`] call takes the next turn. A call on an already
/// cancelled token fails with [`ProviderError::Cancelled`] without taking a
/// turn, and a stream ends with that error as soon as its token is
/// cancelled.
#[derive(Debug)]
pub struct ScriptedProvider {
    state: Mutex<State>,
    /// The context window it reports for every model.
    window: u64,
}

#[derive(Debug)]
struct State {
    turns: VecDeque<ScriptedTurn>,
    consumed: usize,
    record: bool,
    requests: Vec<ProviderRequest>,
}

impl ScriptedProvider {
    /// A provider that replays `turns` in order.
    pub fn new(turns: impl IntoIterator<Item = ScriptedTurn>) -> Self {
        Self {
            state: Mutex::new(State {
                turns: turns.into_iter().collect(),
                consumed: 0,
                record: true,
                requests: Vec::new(),
            }),
            window: DEFAULT_CONTEXT_WINDOW,
        }
    }

    /// Report `window` as every model's context window (128000 unless
    /// set), for tests of what derives from it.
    pub fn with_context_window(mut self, window: u64) -> Self {
        self.window = window;
        self
    }

    /// Stop recording requests, so a long benchmark run does not keep every
    /// request alive.
    pub fn without_recording(self) -> Self {
        self.state().record = false;
        self
    }

    /// Every request received so far, in call order.
    pub fn requests(&self) -> Vec<ProviderRequest> {
        self.state().requests.clone()
    }

    /// Turns not yet consumed.
    pub fn remaining(&self) -> usize {
        self.state().turns.len()
    }

    fn state(&self) -> MutexGuard<'_, State> {
        // No code panics while holding the lock, so a poisoned state is
        // still consistent.
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn take_turn(
        &self,
        request: ProviderRequest,
        cancelled: bool,
    ) -> Result<ScriptedTurn, ProviderError> {
        let mut state = self.state();
        if state.record {
            state.requests.push(request);
        }
        if cancelled {
            return Err(ProviderError::Cancelled);
        }
        let consumed = state.consumed;
        let turn = state.turns.pop_front().ok_or(OutOfTurns { consumed })?;
        state.consumed += 1;
        Ok(turn)
    }
}

impl From<ScriptedFixture> for ScriptedProvider {
    fn from(fixture: ScriptedFixture) -> Self {
        Self::new(fixture.turns)
    }
}

#[async_trait]
impl Provider for ScriptedProvider {
    fn id(&self) -> &str {
        "scripted"
    }

    fn context_window(&self, _model: &str) -> u64 {
        self.window
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let turn = self.take_turn(request, cancel.is_cancelled())?;
        match turn {
            ScriptedTurn {
                events,
                error: Some(error),
            } if events.is_empty() => Err(error.into()),
            turn => Ok(replay(turn, cancel)),
        }
    }
}

/// Stream a turn's events, then its error; end early with
/// [`ProviderError::Cancelled`] once `cancel` fires.
fn replay(turn: ScriptedTurn, cancel: CancellationToken) -> ProviderStream {
    let items = turn
        .events
        .into_iter()
        .map(Ok)
        .chain(turn.error.map(|error| Err(error.into())));
    stream::unfold(
        (items, cancel, false),
        |(mut items, cancel, done)| async move {
            if done {
                return None;
            }
            if cancel.is_cancelled() {
                return Some((Err(ProviderError::Cancelled), (items, cancel, true)));
            }
            let item = items.next()?;
            Some((item, (items, cancel, false)))
        },
    )
    .boxed()
}

/// Builds a well-formed [`ScriptedTurn`]: `message_start`, the content added
/// in call order, then `message_end`.
///
/// The stop reason is `tool_use` when the turn has a tool call and
/// `end_turn` otherwise, unless [`TurnBuilder::stop_reason`] overrides it.
#[derive(Debug, Clone)]
pub struct TurnBuilder {
    events: Vec<ProviderEvent>,
    usage: Usage,
    stop_reason: Option<StopReason>,
    input_chunk: usize,
    has_tool_call: bool,
}

impl TurnBuilder {
    /// Start a turn whose `message_start` names `model`.
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            events: vec![ProviderEvent::MessageStart(MessageStartEvent {
                model: model.into(),
            })],
            usage: Usage::default(),
            stop_reason: None,
            input_chunk: 0,
            has_tool_call: false,
        }
    }

    /// Add one text delta.
    pub fn text(mut self, text: impl Into<String>) -> Self {
        self.events.push(ProviderEvent::TextDelta(TextDeltaEvent {
            text: text.into(),
        }));
        self
    }

    /// Add one thinking delta, optionally signed.
    pub fn thinking(mut self, text: impl Into<String>, signature: Option<&str>) -> Self {
        self.events
            .push(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
                text: text.into(),
                signature: signature.map(str::to_owned),
            }));
        self
    }

    /// Split the input of every later tool call into deltas of at most
    /// `chars` characters; 0 sends each input as one delta.
    pub fn input_chunk(mut self, chars: usize) -> Self {
        self.input_chunk = chars;
        self
    }

    /// Add a complete tool call: start, input deltas, end.
    ///
    /// `input` is sent as compact JSON; a non-object input scripts a
    /// contract violation.
    pub fn tool_call(mut self, id: &str, name: &str, input: &JsonValue) -> Self {
        self.has_tool_call = true;
        self.events
            .push(ProviderEvent::ToolUseStart(ToolUseStartEvent {
                id: id.to_owned(),
                name: name.to_owned(),
            }));
        for json_delta in chunks(&input.to_string(), self.input_chunk) {
            self.events
                .push(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                    id: id.to_owned(),
                    json_delta,
                }));
        }
        self.events.push(ProviderEvent::ToolUseEnd(ToolUseEndEvent {
            id: id.to_owned(),
        }));
        self
    }

    /// Set the usage reported in `message_end`.
    pub fn usage(mut self, usage: Usage) -> Self {
        self.usage = usage;
        self
    }

    /// Override the stop reason reported in `message_end`.
    pub fn stop_reason(mut self, stop_reason: StopReason) -> Self {
        self.stop_reason = Some(stop_reason);
        self
    }

    /// Finish the turn with its `message_end`.
    pub fn build(mut self) -> ScriptedTurn {
        let default_stop = if self.has_tool_call {
            StopReason::ToolUse
        } else {
            StopReason::EndTurn
        };
        self.events.push(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason: self.stop_reason.unwrap_or(default_stop),
            usage: Some(self.usage),
            provider_metadata: None,
        }));
        ScriptedTurn {
            events: self.events,
            error: None,
        }
    }
}

/// Split `text` into pieces of at most `size` characters, on character
/// boundaries; `size == 0` keeps it whole.
fn chunks(text: &str, size: usize) -> Vec<String> {
    if size == 0 {
        return vec![text.to_owned()];
    }
    let mut pieces = Vec::new();
    let mut start = 0;
    for (count, (index, _)) in text.char_indices().enumerate() {
        if count > 0 && count % size == 0 {
            pieces.push(text[start..index].to_owned());
            start = index;
        }
    }
    pieces.push(text[start..].to_owned());
    pieces
}
