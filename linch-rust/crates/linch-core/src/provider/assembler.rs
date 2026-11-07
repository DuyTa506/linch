//! Assembles one assistant turn from a provider's normalized event stream,
//! enforcing the stream contract documented on [`Assembler`].

use std::collections::{HashMap, HashSet};

use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, TextBlock, ThinkingBlock, ToolUseBlock};
use crate::run::StopReason;
use crate::usage::Usage;

use super::event::{MessageEndEvent, ProviderEvent};

mod error;
#[cfg(test)]
mod tests;

pub use error::StreamError;

/// The assembled assistant turn: one message plus provider accounting.
#[derive(Debug, Clone, PartialEq)]
pub struct AssistantAssembly {
    /// The assistant message built from the stream's content blocks.
    pub message: Message,
    /// Why the response stopped.
    pub stop_reason: StopReason,
    /// Token usage reported for the response; `None` when the provider
    /// reported none.
    pub usage: Option<Usage>,
    /// Model that actually served the call, from `message_start`.
    pub model: String,
}

/// A tool call that has been opened but not yet closed.
struct OpenCall {
    name: String,
    input: String,
}

/// Incrementally consumes a provider's [`ProviderEvent`] stream, enforcing
/// the stream contract and coalescing deltas into content blocks as it goes.
///
/// Construct with [`Assembler::new`], push every event in stream order with
/// [`Assembler::push`], then call [`Assembler::finish`]. [`assemble`] does
/// all three for a complete, already-collected stream.
///
/// # Contract
///
/// A valid stream has exactly one `message_start` first and exactly one
/// `message_end` last, with nothing pushed after it. Every `tool_use_start`
/// has a non-empty id (unique across the whole stream) and a non-empty
/// name; an input delta or `tool_use_end` must name a call that is
/// currently open; every opened call must end before `message_end`, and its
/// concatenated input deltas must parse as a JSON object. `message_end`'s
/// `stop_reason` is `tool_use` if and only if at least one tool call
/// completed.
///
/// A `max_tokens` stop is the exception: the output limit may cut a call
/// off, so a call still open, or whose input is not a whole JSON object, is
/// dropped instead. The turn then stops with `max_tokens` when no call is
/// left, and with `tool_use` when calls that finished remain. So a provider
/// reports `max_tokens` as it came and keeps streaming tool input; on any
/// other stop such a call fails the stream at `message_end`.
///
/// # Coalescing
///
/// Consecutive `text_delta`s merge into one [`TextBlock`]; consecutive
/// `thinking_delta`s merge into one [`ThinkingBlock`] until a delta that
/// carries a signature, which ends the block: a provider signs each block
/// once, at its end, and two signed blocks in a row must replay as two. A
/// run of either kind also flushes as soon as a different kind of event
/// arrives, or at `message_end` — so a `text_delta` received while a tool
/// call is open does not flush around that call; the resulting text block
/// simply lands after the tool-use block in `content` order. Each
/// `redacted_thinking` and each completed tool call appends its own block
/// immediately and does not coalesce with neighbors.
///
/// A thinking run flushes to a block if it has any text, or carries a
/// signature — a signed `thinking_delta` with empty text is valid provider
/// output and its signature must still be preserved and replayed, even
/// though the block's `thinking` text is empty. Only a run that is both
/// textless and unsigned flushes to nothing; its signature is still reset
/// so it cannot leak into the next run.
#[derive(Default)]
pub struct Assembler {
    message_started: bool,
    message_ended: bool,
    model: String,
    content: Vec<ContentBlock>,
    text_buf: String,
    thinking_buf: String,
    thinking_sig: Option<String>,
    open_calls: HashMap<String, OpenCall>,
    /// Why the first closed call with broken input is invalid; reported at
    /// `message_end` unless the output limit cut it off.
    broken_call: Option<StreamError>,
    seen_tool_ids: HashSet<String>,
    stop_reason: Option<StopReason>,
    usage: Option<Usage>,
    provider_metadata: Option<JsonObject>,
}

impl Assembler {
    /// A fresh assembler with no events pushed yet.
    pub fn new() -> Self {
        Self::default()
    }

    fn flush_text(&mut self) {
        if !self.text_buf.is_empty() {
            self.content.push(ContentBlock::Text(TextBlock {
                text: std::mem::take(&mut self.text_buf),
            }));
        }
    }

    fn flush_thinking(&mut self) {
        // Always take the signature, resetting it for the next run. Emit a
        // block when the run has text or a signature; a run that is both
        // textless and unsigned carries no information and emits nothing,
        // but its (absent) signature must not leak into the next run
        // either, hence taking it unconditionally above.
        let signature = self.thinking_sig.take();
        if !self.thinking_buf.is_empty() || signature.is_some() {
            self.content.push(ContentBlock::Thinking(ThinkingBlock {
                thinking: std::mem::take(&mut self.thinking_buf),
                signature,
            }));
        }
    }

    /// Feed the next event from the stream.
    ///
    /// # Errors
    ///
    /// Returns the [`StreamError`] naming the first contract rule this event
    /// violates; see the type-level docs for the full rule list.
    pub fn push(&mut self, event: ProviderEvent) -> Result<(), StreamError> {
        if self.message_ended {
            return Err(StreamError::EventAfterEnd { kind: event.kind() });
        }
        if let ProviderEvent::MessageStart(start) = event {
            if self.message_started {
                return Err(StreamError::DuplicateMessageStart);
            }
            if start.model.trim().is_empty() {
                return Err(StreamError::BlankModel);
            }
            self.model = start.model;
            self.message_started = true;
            return Ok(());
        }
        if !self.message_started {
            return Err(StreamError::MissingMessageStart { kind: event.kind() });
        }
        self.push_content_event(event)
    }

    fn push_content_event(&mut self, event: ProviderEvent) -> Result<(), StreamError> {
        match event {
            ProviderEvent::MessageStart(_) => unreachable!("handled by push"),
            ProviderEvent::TextDelta(delta) => {
                self.flush_thinking();
                self.text_buf.push_str(&delta.text);
                Ok(())
            }
            ProviderEvent::ThinkingDelta(delta) => {
                self.flush_text();
                self.thinking_buf.push_str(&delta.text);
                if let Some(signature) = delta.signature {
                    self.thinking_sig = Some(signature);
                    self.flush_thinking();
                }
                Ok(())
            }
            ProviderEvent::RedactedThinking(block) => {
                self.flush_text();
                self.flush_thinking();
                self.content.push(ContentBlock::RedactedThinking(block));
                Ok(())
            }
            ProviderEvent::ToolUseStart(start) => self.push_tool_use_start(start),
            ProviderEvent::ToolUseInputDelta(delta) => self.push_tool_use_input_delta(delta),
            ProviderEvent::ToolUseEnd(end) => self.push_tool_use_end(end),
            ProviderEvent::MessageEnd(end) => self.push_message_end(end),
        }
    }

    fn push_tool_use_start(
        &mut self,
        start: super::event::ToolUseStartEvent,
    ) -> Result<(), StreamError> {
        self.flush_text();
        self.flush_thinking();
        if start.id.is_empty() {
            return Err(StreamError::EmptyToolId);
        }
        if start.name.is_empty() {
            return Err(StreamError::EmptyToolName);
        }
        if !self.seen_tool_ids.insert(start.id.clone()) {
            return Err(StreamError::DuplicateToolId { id: start.id });
        }
        self.open_calls.insert(
            start.id,
            OpenCall {
                name: start.name,
                input: String::new(),
            },
        );
        Ok(())
    }

    fn push_tool_use_input_delta(
        &mut self,
        delta: super::event::ToolUseInputDeltaEvent,
    ) -> Result<(), StreamError> {
        match self.open_calls.get_mut(&delta.id) {
            Some(call) => {
                call.input.push_str(&delta.json_delta);
                Ok(())
            }
            None => Err(StreamError::UnknownToolId {
                id: delta.id,
                event: "tool_use_input_delta",
            }),
        }
    }

    fn push_tool_use_end(&mut self, end: super::event::ToolUseEndEvent) -> Result<(), StreamError> {
        let Some(call) = self.open_calls.remove(&end.id) else {
            return Err(StreamError::UnknownToolId {
                id: end.id,
                event: "tool_use_end",
            });
        };
        let id = end.id;
        match serde_json::from_str(&call.input) {
            Ok(serde_json::Value::Object(input)) => {
                let name = call.name;
                self.content
                    .push(ContentBlock::ToolUse(ToolUseBlock { id, name, input }));
            }
            // Judged at `message_end`, whose stop reason says whether the
            // output limit cut the call off.
            Ok(_) => {
                self.broken_call
                    .get_or_insert(StreamError::NonObjectToolInput { id });
            }
            Err(source) => {
                self.broken_call
                    .get_or_insert(StreamError::InvalidToolInput { id, source });
            }
        }
        Ok(())
    }

    fn push_message_end(&mut self, end: MessageEndEvent) -> Result<(), StreamError> {
        // Destructure up front: `usage`/`stop_reason` are the event's own
        // observed accounting, not a side effect of the checks below, so
        // every end-shape error can carry them without re-reading `end`.
        let MessageEndEvent {
            stop_reason,
            usage,
            provider_metadata,
        } = end;
        if stop_reason == StopReason::MaxTokens {
            // Calls the output limit cut off.
            self.open_calls.clear();
            self.broken_call = None;
        }
        if let Some(error) = self.broken_call.take() {
            return Err(error);
        }
        if !self.open_calls.is_empty() {
            let mut ids: Vec<String> = self.open_calls.keys().cloned().collect();
            ids.sort();
            return Err(StreamError::UnfinishedToolCalls {
                ids,
                usage,
                stop_reason,
            });
        }
        self.flush_text();
        self.flush_thinking();
        let has_tool_use = self
            .content
            .iter()
            .any(|block| matches!(block, ContentBlock::ToolUse(_)));
        let stop_reason = match stop_reason {
            StopReason::MaxTokens if has_tool_use => StopReason::ToolUse,
            other => other,
        };
        if stop_reason == StopReason::ToolUse && !has_tool_use {
            return Err(StreamError::ToolUseWithoutCalls { usage, stop_reason });
        }
        if stop_reason != StopReason::ToolUse && has_tool_use {
            return Err(StreamError::CallsWithoutToolUse { stop_reason, usage });
        }
        self.stop_reason = Some(stop_reason);
        self.usage = usage;
        self.provider_metadata = provider_metadata;
        self.message_ended = true;
        Ok(())
    }

    /// Finish the stream, returning the assembled turn.
    ///
    /// # Errors
    ///
    /// [`StreamError::EndedBeforeStart`] if no `message_start` was ever
    /// pushed; [`StreamError::EndedBeforeEnd`] if the stream stopped before
    /// a `message_end` was pushed.
    pub fn finish(self) -> Result<AssistantAssembly, StreamError> {
        if !self.message_started {
            return Err(StreamError::EndedBeforeStart);
        }
        if !self.message_ended {
            return Err(StreamError::EndedBeforeEnd);
        }
        Ok(AssistantAssembly {
            message: Message {
                role: Role::Assistant,
                content: self.content,
                provider_metadata: self.provider_metadata,
            },
            stop_reason: self
                .stop_reason
                .expect("message_ended implies stop_reason is set"),
            usage: self.usage,
            model: self.model,
        })
    }
}

/// Assemble a complete, already-collected stream in one call.
///
/// Equivalent to pushing every event onto a fresh [`Assembler`] in order and
/// calling [`Assembler::finish`].
///
/// # Errors
///
/// The [`StreamError`] from the first event that violates the contract, or
/// from `finish` if the stream never reached `message_start`/`message_end`.
pub fn assemble(
    events: impl IntoIterator<Item = ProviderEvent>,
) -> Result<AssistantAssembly, StreamError> {
    let mut assembler = Assembler::new();
    for event in events {
        assembler.push(event)?;
    }
    assembler.finish()
}
