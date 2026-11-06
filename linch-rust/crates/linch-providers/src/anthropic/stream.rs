//! Messages SSE events to normalized [`ProviderEvent`]s.
//!
//! [`Translator`] is pure: it turns one event's `data` at a time into
//! events. [`events`] drives it over a byte stream with cancellation.

use std::collections::{HashMap, VecDeque};

use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{ProviderError, ProviderEvent, RedactedThinkingBlock, StopReason, Usage};

use super::http;
use crate::ApiKey;
use wire::{BareError, Wire, WireBlock, WireDelta, WireMessage, WireUsage};

mod driver;
mod wire;

pub(crate) use driver::events;

/// An open content block, by its stream index.
enum Block {
    Text,
    Thinking,
    Tool {
        id: String,
        /// Input carried on the start frame, sent if no delta follows.
        initial: Option<String>,
        has_input: bool,
    },
    /// A block type Linch does not model; its deltas are dropped.
    Skipped,
}

/// Turns event payloads into events for one response.
pub(crate) struct Translator {
    fallback_model: String,
    key: ApiKey,
    started: bool,
    blocks: HashMap<u64, Block>,
    tool_calls: usize,
    usage: Option<Usage>,
    stop: Option<StopReason>,
}

impl Translator {
    /// `fallback_model` names the model when `message_start` has none.
    pub(crate) fn new(fallback_model: String, key: ApiKey) -> Self {
        Self {
            fallback_model,
            key,
            started: false,
            blocks: HashMap::new(),
            tool_calls: 0,
            usage: None,
            stop: None,
        }
    }

    /// Translate one event's `data`. Returns `true` at `message_stop`.
    pub(crate) fn push(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<bool, ProviderError> {
        let event = match serde_json::from_str::<Wire>(data) {
            Ok(event) => event,
            Err(err) => {
                return Err(match serde_json::from_str::<BareError>(data) {
                    Ok(bare) => http::stream_error(&bare.error, &self.key),
                    Err(_) => transport(&format!("malformed stream event: {err}")),
                });
            }
        };
        match event {
            Wire::MessageStart { message } => self.start(message, out)?,
            Wire::Error { error } => return Err(http::stream_error(&error, &self.key)),
            Wire::Other => {}
            _ if !self.started => {
                return Err(transport("stream event arrived before message_start"));
            }
            Wire::ContentBlockStart {
                index,
                content_block,
            } => self.block_start(index, content_block, out)?,
            Wire::ContentBlockDelta { index, delta } => self.delta(index, delta, out)?,
            Wire::ContentBlockStop { index } => self.block_stop(index, out)?,
            Wire::MessageDelta { delta, usage } => {
                if let Some(reason) = delta.stop_reason {
                    self.stop = Some(stop_reason(&reason)?);
                }
                if let Some(usage) = usage {
                    self.add_usage(&usage);
                }
            }
            Wire::MessageStop => {
                self.finish(out)?;
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn start(
        &mut self,
        message: WireMessage,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        if self.started {
            return Err(transport("duplicate message_start"));
        }
        self.started = true;
        let model = message
            .model
            .filter(|model| !model.trim().is_empty())
            .unwrap_or_else(|| self.fallback_model.clone());
        if let Some(usage) = message.usage {
            self.add_usage(&usage);
        }
        out.push_back(ProviderEvent::MessageStart(MessageStartEvent { model }));
        Ok(())
    }

    fn add_usage(&mut self, wire: &WireUsage) {
        let usage = self.usage.get_or_insert_with(Usage::default);
        for (field, value) in [
            (&mut usage.input_tokens, wire.input_tokens),
            (&mut usage.output_tokens, wire.output_tokens),
            (&mut usage.cache_read_tokens, wire.cache_read_input_tokens),
            (
                &mut usage.cache_creation_tokens,
                wire.cache_creation_input_tokens,
            ),
        ] {
            if let Some(value) = value {
                *field = value;
            }
        }
    }

    fn block_start(
        &mut self,
        index: u64,
        block: WireBlock,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let block = match block {
            WireBlock::Text { text } => {
                push_text(out, text);
                Block::Text
            }
            WireBlock::Thinking {
                thinking,
                signature,
            } => {
                push_thinking(out, thinking, signature);
                Block::Thinking
            }
            WireBlock::RedactedThinking { data } => {
                out.push_back(ProviderEvent::RedactedThinking(RedactedThinkingBlock {
                    data,
                }));
                Block::Skipped
            }
            WireBlock::ToolUse { id, name, input } => {
                out.push_back(ProviderEvent::ToolUseStart(ToolUseStartEvent {
                    id: id.clone(),
                    name,
                }));
                let initial = input
                    .filter(|input| input.as_object().is_some_and(|map| !map.is_empty()))
                    .map(|input| input.to_string());
                Block::Tool {
                    id,
                    initial,
                    has_input: false,
                }
            }
            WireBlock::Other => Block::Skipped,
        };
        if self.blocks.insert(index, block).is_some() {
            return Err(transport(&format!("content block {index} started twice")));
        }
        Ok(())
    }

    fn delta(
        &mut self,
        index: u64,
        delta: WireDelta,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let Some(block) = self.blocks.get_mut(&index) else {
            return Err(transport(&format!(
                "delta for unopened content block {index}"
            )));
        };
        match (block, delta) {
            (Block::Skipped, _) | (_, WireDelta::Other) => {}
            (Block::Text, WireDelta::TextDelta { text }) => push_text(out, text),
            (Block::Thinking, WireDelta::ThinkingDelta { thinking }) => {
                push_thinking(out, thinking, String::new());
            }
            (Block::Thinking, WireDelta::SignatureDelta { signature }) => {
                push_thinking(out, String::new(), signature);
            }
            (Block::Tool { id, has_input, .. }, WireDelta::InputJsonDelta { partial_json }) => {
                if !partial_json.is_empty() {
                    *has_input = true;
                    out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                        id: id.clone(),
                        json_delta: partial_json,
                    }));
                }
            }
            _ => {
                return Err(transport(&format!(
                    "delta does not match content block {index}"
                )));
            }
        }
        Ok(())
    }

    /// A tool call with no input deltas sends its start-frame input, or
    /// `{}`, then ends.
    fn block_stop(
        &mut self,
        index: u64,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        match self.blocks.remove(&index) {
            None => Err(transport(&format!(
                "stop for unopened content block {index}"
            ))),
            Some(Block::Tool {
                id,
                initial,
                has_input,
            }) => {
                if !has_input {
                    out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                        id: id.clone(),
                        json_delta: initial.unwrap_or_else(|| "{}".into()),
                    }));
                }
                out.push_back(ProviderEvent::ToolUseEnd(ToolUseEndEvent { id }));
                self.tool_calls += 1;
                Ok(())
            }
            Some(_) => Ok(()),
        }
    }

    /// Close the response at `message_stop`. Any completed tool call makes
    /// the stop reason `tool_use`.
    fn finish(&mut self, out: &mut VecDeque<ProviderEvent>) -> Result<(), ProviderError> {
        let Some(stop) = self.stop else {
            return Err(transport("message_stop arrived without a stop_reason"));
        };
        let stop_reason = if self.tool_calls > 0 {
            StopReason::ToolUse
        } else {
            stop
        };
        out.push_back(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason,
            usage: self.usage,
            provider_metadata: None,
        }));
        Ok(())
    }
}

fn push_text(out: &mut VecDeque<ProviderEvent>, text: String) {
    if !text.is_empty() {
        out.push_back(ProviderEvent::TextDelta(TextDeltaEvent { text }));
    }
}

/// A signature rides on a textless thinking delta; the assembler keeps it
/// on the block even when the block has no text.
fn push_thinking(out: &mut VecDeque<ProviderEvent>, text: String, signature: String) {
    if !text.is_empty() || !signature.is_empty() {
        out.push_back(ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
            text,
            signature: (!signature.is_empty()).then_some(signature),
        }));
    }
}

/// `stop_reason` to a stop reason. `pause_turn` (server tools only) and
/// unknown values fail the call as a retryable transport error.
fn stop_reason(reason: &str) -> Result<StopReason, ProviderError> {
    match reason {
        "end_turn" => Ok(StopReason::EndTurn),
        "tool_use" => Ok(StopReason::ToolUse),
        "max_tokens" | "model_context_window_exceeded" => Ok(StopReason::MaxTokens),
        "stop_sequence" => Ok(StopReason::StopSequence),
        "refusal" => Ok(StopReason::Refusal),
        other => Err(transport(&format!(
            "response ended with stop_reason {other:?}"
        ))),
    }
}

fn transport(message: &str) -> ProviderError {
    ProviderError::Transport {
        message: message.into(),
    }
}

#[cfg(test)]
mod tests;
