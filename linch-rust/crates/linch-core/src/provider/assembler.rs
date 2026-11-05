//! Assembles one assistant turn from a provider's normalized event stream,
//! enforcing the stream contract documented on [`Assembler`].

use std::collections::{HashMap, HashSet};

use crate::json::JsonObject;
use crate::message::{ContentBlock, Message, Role, TextBlock, ThinkingBlock, ToolUseBlock};
use crate::run::StopReason;
use crate::usage::Usage;

use super::event::{MessageEndEvent, ProviderEvent};

#[cfg(test)]
mod tests;

/// The assembled assistant turn: one message plus provider accounting.
#[derive(Debug, Clone, PartialEq)]
pub struct AssistantAssembly {
    /// The assistant message built from the stream's content blocks.
    pub message: Message,
    /// Why the response stopped.
    pub stop_reason: StopReason,
    /// Token usage reported for the response.
    pub usage: Usage,
    /// Model that actually served the call, from `message_start`.
    pub model: String,
}

/// A violation of the provider stream contract, naming the rule broken.
#[derive(Debug, thiserror::Error)]
pub enum StreamError {
    /// A second `message_start` was pushed.
    #[error("duplicate message_start")]
    DuplicateMessageStart,
    /// An event other than `message_start` arrived before one was pushed.
    #[error("event {kind:?} arrived before message_start")]
    MissingMessageStart {
        /// Wire `type` of the offending event.
        kind: &'static str,
    },
    /// `message_start.model` was empty or all-whitespace.
    #[error("message_start.model must be a nonblank string")]
    BlankModel,
    /// An event arrived after `message_end`.
    #[error("event {kind:?} arrived after message_end")]
    EventAfterEnd {
        /// Wire `type` of the offending event.
        kind: &'static str,
    },
    /// `tool_use_start.id` was empty.
    #[error("tool_use_start.id must not be empty")]
    EmptyToolId,
    /// `tool_use_start.name` was empty.
    #[error("tool_use_start.name must not be empty")]
    EmptyToolName,
    /// Two `tool_use_start` events in the same stream used the same id.
    #[error("duplicate tool-use id {id:?}")]
    DuplicateToolId {
        /// The repeated id.
        id: String,
    },
    /// An input delta or `tool_use_end` named an id with no open call.
    #[error("{event} references an id with no open call: {id:?}")]
    UnknownToolId {
        /// The offending id.
        id: String,
        /// Which event referenced it.
        event: &'static str,
    },
    /// `message_end` arrived while one or more tool calls were still open.
    #[error("message_end arrived before tool_use_end for {ids:?}")]
    UnfinishedToolCalls {
        /// Ids of the tool calls that never ended, sorted.
        ids: Vec<String>,
    },
    /// A tool call's concatenated input deltas did not parse as JSON.
    #[error("tool {id:?} input is invalid JSON: {source}")]
    InvalidToolInput {
        /// The tool call's id.
        id: String,
        /// The underlying JSON parse error.
        #[source]
        source: serde_json::Error,
    },
    /// A tool call's input parsed but was not a JSON object.
    #[error("tool {id:?} input must decode to a JSON object")]
    NonObjectToolInput {
        /// The tool call's id.
        id: String,
    },
    /// The stream ended without ever pushing a `message_start`.
    #[error("stream ended before message_start")]
    EndedBeforeStart,
    /// The stream ended without ever pushing a `message_end`.
    #[error("stream ended before message_end")]
    EndedBeforeEnd,
    /// `stop_reason` was `tool_use` but no tool call completed.
    #[error("stop_reason='tool_use' requires at least one completed tool call")]
    ToolUseWithoutCalls,
    /// One or more tool calls completed but `stop_reason` was not `tool_use`.
    #[error("completed tool calls but stop_reason was {stop_reason:?}, not tool_use")]
    CallsWithoutToolUse {
        /// The stop reason actually reported.
        stop_reason: StopReason,
    },
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
/// # Coalescing
///
/// Consecutive `text_delta`s merge into one [`TextBlock`]; consecutive
/// `thinking_delta`s merge into one [`ThinkingBlock`] whose `signature` is
/// the last non-`None` signature seen in that run. A run of either kind
/// flushes as soon as a different kind of event arrives, or at
/// `message_end` — so a `text_delta` received while a tool call is open
/// does not flush around that call; the resulting text block simply lands
/// after the tool-use block in `content` order. Each `redacted_thinking`
/// and each completed tool call appends its own block immediately and does
/// not coalesce with neighbors.
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
        let parsed: serde_json::Value =
            serde_json::from_str(&call.input).map_err(|source| StreamError::InvalidToolInput {
                id: end.id.clone(),
                source,
            })?;
        let serde_json::Value::Object(input) = parsed else {
            return Err(StreamError::NonObjectToolInput { id: end.id });
        };
        self.content.push(ContentBlock::ToolUse(ToolUseBlock {
            id: end.id,
            name: call.name,
            input,
        }));
        Ok(())
    }

    fn push_message_end(&mut self, end: MessageEndEvent) -> Result<(), StreamError> {
        if !self.open_calls.is_empty() {
            let mut ids: Vec<String> = self.open_calls.keys().cloned().collect();
            ids.sort();
            return Err(StreamError::UnfinishedToolCalls { ids });
        }
        self.flush_text();
        self.flush_thinking();
        let has_tool_use = self
            .content
            .iter()
            .any(|block| matches!(block, ContentBlock::ToolUse(_)));
        if end.stop_reason == StopReason::ToolUse && !has_tool_use {
            return Err(StreamError::ToolUseWithoutCalls);
        }
        if end.stop_reason != StopReason::ToolUse && has_tool_use {
            return Err(StreamError::CallsWithoutToolUse {
                stop_reason: end.stop_reason,
            });
        }
        self.stop_reason = Some(end.stop_reason);
        self.usage = Some(end.usage);
        self.provider_metadata = end.provider_metadata;
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
            usage: self.usage.expect("message_ended implies usage is set"),
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
