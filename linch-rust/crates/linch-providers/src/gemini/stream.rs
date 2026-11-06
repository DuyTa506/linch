//! `streamGenerateContent` SSE chunks to normalized [`ProviderEvent`]s.
//!
//! [`Translator`] is pure: it turns one chunk at a time into events.
//! [`events`] drives it over a byte stream with cancellation. Gemini sends
//! no end marker: the response is complete when the stream closes after a
//! `finishReason`.

use std::collections::VecDeque;

use linch_core::provider::{
    MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, ToolUseEndEvent,
    ToolUseInputDeltaEvent, ToolUseStartEvent,
};
use linch_core::{JsonObject, JsonValue, ProviderError, ProviderEvent, StopReason, Usage};
use serde::Deserialize;

use super::{PARTS_KEY, http};
use crate::ApiKey;

mod driver;

pub(crate) use driver::events;

// Vendor chunks are read leniently: unknown fields are ignored. Parts stay
// raw JSON because they are kept verbatim for replay.

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Chunk {
    #[serde(default)]
    candidates: Option<Vec<Candidate>>,
    #[serde(default)]
    usage_metadata: Option<UsageMetadata>,
    #[serde(default)]
    model_version: Option<String>,
    #[serde(default)]
    prompt_feedback: Option<PromptFeedback>,
    #[serde(default)]
    error: Option<JsonValue>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Candidate {
    #[serde(default)]
    content: Option<Content>,
    #[serde(default)]
    finish_reason: Option<String>,
}

#[derive(Deserialize)]
struct Content {
    #[serde(default)]
    parts: Option<Vec<JsonValue>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PromptFeedback {
    #[serde(default)]
    block_reason: Option<String>,
}

/// Token counts; each chunk restates them, so the last one wins.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct UsageMetadata {
    #[serde(default)]
    prompt_token_count: u64,
    #[serde(default)]
    candidates_token_count: u64,
    #[serde(default)]
    thoughts_token_count: u64,
    #[serde(default)]
    cached_content_token_count: u64,
    #[serde(default)]
    tool_use_prompt_token_count: u64,
}

impl From<UsageMetadata> for Usage {
    /// The prompt count includes cached tokens and output excludes
    /// thoughts; Linch counts cache reads apart and thoughts as output.
    fn from(wire: UsageMetadata) -> Self {
        Usage {
            input_tokens: wire
                .prompt_token_count
                .saturating_sub(wire.cached_content_token_count)
                .saturating_add(wire.tool_use_prompt_token_count),
            output_tokens: wire
                .candidates_token_count
                .saturating_add(wire.thoughts_token_count),
            cache_read_tokens: wire.cached_content_token_count,
            cache_creation_tokens: 0,
        }
    }
}

/// Turns chunks into events for one response.
pub(crate) struct Translator {
    fallback_model: String,
    /// Assistant turns before this response; part of synthesized call ids,
    /// so they stay unique across a conversation.
    turn: usize,
    key: ApiKey,
    started: bool,
    calls: usize,
    /// Whether a part carried a thought signature or a call carried an id;
    /// either means the raw parts must be replayed.
    keep: bool,
    parts: Vec<JsonValue>,
    finish: Option<StopReason>,
    usage: Option<Usage>,
}

impl Translator {
    /// `fallback_model` names the model when no chunk reports one; `turn`
    /// counts the request's earlier assistant turns.
    pub(crate) fn new(fallback_model: String, turn: usize, key: ApiKey) -> Self {
        Self {
            fallback_model,
            turn,
            key,
            started: false,
            calls: 0,
            keep: false,
            parts: Vec::new(),
            finish: None,
            usage: None,
        }
    }

    /// Translate one `data:` payload.
    pub(crate) fn push(
        &mut self,
        data: &str,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        let chunk: Chunk = serde_json::from_str(data)
            .map_err(|err| transport(&format!("malformed stream chunk: {err}")))?;
        if let Some(error) = &chunk.error {
            return Err(http::stream_error(error, &self.key));
        }
        if !self.started {
            self.started = true;
            let model = chunk
                .model_version
                .filter(|model| !model.trim().is_empty())
                .unwrap_or_else(|| self.fallback_model.clone());
            out.push_back(ProviderEvent::MessageStart(MessageStartEvent { model }));
        }
        if let Some(usage) = chunk.usage_metadata {
            self.usage = Some(usage.into());
        }
        // A blocked prompt is answered with no candidates at all.
        if chunk
            .prompt_feedback
            .and_then(|feedback| feedback.block_reason)
            .is_some()
        {
            self.finish = Some(StopReason::Refusal);
        }
        let Some(candidate) = chunk.candidates.and_then(|all| all.into_iter().next()) else {
            return Ok(());
        };
        let parts = candidate.content.and_then(|content| content.parts);
        for part in parts.unwrap_or_default() {
            self.part(part, out);
        }
        if let Some(reason) = candidate.finish_reason {
            self.finish = Some(finish_reason(&reason)?);
        }
        Ok(())
    }

    fn part(&mut self, part: JsonValue, out: &mut VecDeque<ProviderEvent>) {
        self.keep |= part.get("thoughtSignature").is_some();
        if let Some(call) = part.get("functionCall") {
            let server_id = call
                .get("id")
                .and_then(JsonValue::as_str)
                .filter(|id| !id.is_empty());
            self.keep |= server_id.is_some();
            // Gemini may omit call ids; a synthesized one is fixed by the
            // conversation position, so replays and tests see the same id.
            let id = server_id
                .map(String::from)
                .unwrap_or_else(|| format!("gemini_{}_{}", self.turn, self.calls));
            let name = call.get("name").and_then(JsonValue::as_str);
            out.push_back(ProviderEvent::ToolUseStart(ToolUseStartEvent {
                id: id.clone(),
                name: name.unwrap_or_default().into(),
            }));
            let args = call
                .get("args")
                .map_or_else(|| "{}".into(), JsonValue::to_string);
            out.push_back(ProviderEvent::ToolUseInputDelta(ToolUseInputDeltaEvent {
                id: id.clone(),
                json_delta: args,
            }));
            out.push_back(ProviderEvent::ToolUseEnd(ToolUseEndEvent { id }));
            self.calls += 1;
        } else if let Some(text) = part.get("text").and_then(JsonValue::as_str)
            && !text.is_empty()
        {
            let text = text.to_string();
            out.push_back(if part.get("thought") == Some(&JsonValue::Bool(true)) {
                ProviderEvent::ThinkingDelta(ThinkingDeltaEvent {
                    text,
                    signature: None,
                })
            } else {
                ProviderEvent::TextDelta(TextDeltaEvent { text })
            });
        }
        self.keep_part(part);
    }

    /// Keep `part` for replay. Consecutive plain text parts of one kind
    /// merge; a part with a signature or anything else stays whole.
    fn keep_part(&mut self, part: JsonValue) {
        let thought = part.get("thought").cloned();
        if plain(&part)
            && let Some(previous) = self.parts.last_mut()
            && plain(previous)
            && previous.get("thought").cloned() == thought
            && let (Some(JsonValue::String(head)), Some(tail)) =
                (previous.get_mut("text"), part["text"].as_str())
        {
            head.push_str(tail);
            return;
        }
        self.parts.push(part);
    }

    /// Close the response when the stream ends. Any tool call makes the
    /// stop reason `tool_use`.
    pub(crate) fn finish(
        &mut self,
        out: &mut VecDeque<ProviderEvent>,
    ) -> Result<(), ProviderError> {
        if !self.started {
            return Err(transport("stream closed before any chunk"));
        }
        let Some(finish) = self.finish else {
            return Err(transport("stream closed without a finishReason"));
        };
        let stop_reason = if self.calls > 0 {
            StopReason::ToolUse
        } else {
            finish
        };
        let provider_metadata = self.keep.then(|| {
            let mut metadata = JsonObject::new();
            metadata.insert(PARTS_KEY.into(), std::mem::take(&mut self.parts).into());
            metadata
        });
        out.push_back(ProviderEvent::MessageEnd(MessageEndEvent {
            stop_reason,
            usage: self.usage,
            provider_metadata,
        }));
        Ok(())
    }
}

/// A text part with nothing but `text` and an optional `thought` flag.
fn plain(part: &JsonValue) -> bool {
    part.as_object().is_some_and(|fields| {
        fields.get("text").is_some_and(JsonValue::is_string)
            && fields.keys().all(|key| key == "text" || key == "thought")
    })
}

/// `finishReason` to a stop reason. Content flagged by a filter is a
/// refusal; a missing signature is the adapter's own replay bug; anything
/// else fails the call as a retryable transport error.
fn finish_reason(reason: &str) -> Result<StopReason, ProviderError> {
    match reason {
        "STOP" => Ok(StopReason::EndTurn),
        "MAX_TOKENS" | "CONTINUATION" => Ok(StopReason::MaxTokens),
        "SAFETY"
        | "RECITATION"
        | "LANGUAGE"
        | "BLOCKLIST"
        | "PROHIBITED_CONTENT"
        | "SPII"
        | "IMAGE_SAFETY"
        | "IMAGE_PROHIBITED_CONTENT"
        | "IMAGE_RECITATION"
        | "ESCALATION"
        | "PUP_LIMITED_DISABLED" => Ok(StopReason::Refusal),
        "MISSING_THOUGHT_SIGNATURE" => Err(ProviderError::InvalidRequest {
            message: "Gemini reports a missing thought signature in the request".into(),
        }),
        other => Err(transport(&format!(
            "response ended with finishReason {other:?}"
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
