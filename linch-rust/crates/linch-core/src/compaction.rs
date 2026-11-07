//! Compaction: fold the older part of a long provider view into a summary.
//!
//! When the estimated provider view reaches [`CompactionSpec::threshold_tokens`],
//! or without one comes near the provider's context window,
//! the run asks the model for a summary of everything older than the last
//! [`CompactionSpec::keep_recent_turns`] assistant turns, in an auxiliary
//! call with its own delivery and receipt (kind `compaction`). The new view
//! is a logged projection: the first message, the summary as a user
//! message, then the kept turns. The system prompt and the first message
//! stay byte-identical, so a provider's cached prefix survives.
//!
//! This module holds the pure parts; the runner places the barriers.

use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::agent::AgentSpec;
use crate::message::{ContentBlock, Message, Role, SystemBlock, ToolResultPart};
use crate::provider::{ProviderRequest, ToolChoice, ToolDefinition};

/// Instructions for the summary call when the spec sets none.
pub const DEFAULT_PROMPT: &str = "Summarize the conversation so far. Capture:\n\
    - Decisions made and conclusions reached\n\
    - Key facts, entities, and data established (with identifiers)\n\
    - Actions taken and their outcomes\n\
    - Open questions and current task state\n\
    Keep it factual and dense. Omit greetings, restated goals, and meta commentary.";

/// Reason stored with a compaction's projection entry.
pub(crate) const PROJECTION_REASON: &str = "compaction";

/// Strategy name in the `compaction` event.
pub(crate) const STRATEGY: &str = "summary";

/// Leads the summary message, so the model reads it as a recap.
const SUMMARY_HEADER: &str = "<summary of earlier conversation>\n\n";

/// Rough per-image token cost, as in the Python estimator.
const IMAGE_TOKENS: u64 = 1000;

/// The most output a window-based trigger reserves when the spec sets no
/// output limit (Python's 32k).
const MAX_RESERVE: u64 = 32_768;

fn default_keep_recent_turns() -> u32 {
    10
}

fn default_max_output_tokens() -> u32 {
    4096
}

/// When and how a run compacts its provider view. Off unless an
/// [`AgentSpec`] sets one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompactionSpec {
    /// Compact before a turn whose provider view is estimated at this many
    /// tokens or more (about four characters per token, 1000 per image).
    /// `None` (the default) follows the provider's context window instead,
    /// as Python does: compact once the estimate plus an output reserve
    /// (the spec's `max_output_tokens`, else a fifth of the window, at
    /// least 1 and at most 32768) reaches 80% of the window.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub threshold_tokens: Option<u64>,
    /// Assistant turns kept verbatim after the summary (default 10, at
    /// least 1).
    #[serde(default = "default_keep_recent_turns")]
    pub keep_recent_turns: u32,
    /// Instructions for the summary call; `None` uses [`DEFAULT_PROMPT`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prompt: Option<String>,
    /// Most tokens the summary may use (default 4096).
    #[serde(default = "default_max_output_tokens")]
    pub max_output_tokens: u32,
}

impl Default for CompactionSpec {
    /// Compaction near the provider's context window, with every other
    /// field at its default.
    fn default() -> Self {
        Self {
            threshold_tokens: None,
            keep_recent_turns: default_keep_recent_turns(),
            prompt: None,
            max_output_tokens: default_max_output_tokens(),
        }
    }
}

impl CompactionSpec {
    /// Compaction at `threshold_tokens`, with every other field at its
    /// default.
    pub fn new(threshold_tokens: u64) -> Self {
        Self {
            threshold_tokens: Some(threshold_tokens),
            ..Self::default()
        }
    }

    /// Why this spec cannot run, if it cannot.
    pub(crate) fn problem(&self) -> Option<&'static str> {
        if self.threshold_tokens == Some(0) {
            return Some("compaction.threshold_tokens must be at least 1");
        }
        if self.keep_recent_turns == 0 {
            return Some("compaction.keep_recent_turns must be at least 1");
        }
        if self.max_output_tokens == 0 {
            return Some("compaction.max_output_tokens must be at least 1");
        }
        None
    }
}

/// Estimated tokens of `messages`: text, tool inputs and tool results at
/// four characters per token, a fixed cost per image. Signatures and
/// redacted reasoning are opaque and not counted.
pub(crate) fn estimate_tokens(messages: &[Arc<Message>]) -> u64 {
    let (mut chars, mut images) = (0usize, 0u64);
    for message in messages {
        for block in &message.content {
            match block {
                ContentBlock::Text(text) => chars += text.text.len(),
                ContentBlock::CatalogChange(change) => chars += change.text.len(),
                ContentBlock::Thinking(thinking) => chars += thinking.thinking.len(),
                ContentBlock::RedactedThinking(_) => {}
                ContentBlock::Image(_) => images += 1,
                ContentBlock::ToolUse(call) => {
                    let input = serde_json::to_string(&call.input).map_or(0, |s| s.len());
                    chars += call.name.len() + input;
                }
                ContentBlock::ToolResult(result) => {
                    for part in &result.content {
                        match part {
                            ToolResultPart::Text(text) => chars += text.text.len(),
                            ToolResultPart::Image(_) => images += 1,
                        }
                    }
                }
            }
        }
    }
    chars as u64 / 4 + images * IMAGE_TOKENS
}

/// Whether a provider view estimated at `tokens` is due for a summary.
///
/// With `threshold_tokens` set, at that many tokens. Without it, Python's
/// rule: when the estimate plus an output reserve reaches 80% of the
/// model's context `window`. The reserve is the spec's `max_output_tokens`
/// when set (not 0), else a fifth of the window, at least 1 and at most
/// 32768, so a small local window is not always over the line.
pub(crate) fn due(
    compaction: &CompactionSpec,
    max_output_tokens: Option<u32>,
    window: u64,
    tokens: u64,
) -> bool {
    if let Some(threshold) = compaction.threshold_tokens {
        return tokens >= threshold;
    }
    let reserve = match max_output_tokens.filter(|&limit| limit > 0) {
        Some(limit) => u64::from(limit),
        None => (window / 5).clamp(1, MAX_RESERVE),
    };
    let projected = u128::from(tokens) + u128::from(reserve);
    projected * 5 >= u128::from(window) * 4
}

/// Where the kept turns start: the `keep_recent_turns`-th assistant message
/// from the end, so no tool result is cut off from its call. `None` when
/// the part between the first message and that point holds no assistant
/// turn, so a summary would fold nothing.
pub(crate) fn keep_from(view: &[Arc<Message>], keep_recent_turns: u32) -> Option<usize> {
    let keep = usize::try_from(keep_recent_turns.max(1)).unwrap_or(usize::MAX);
    let (at, _) = (view.iter().enumerate())
        .rev()
        .filter(|(_, message)| message.role == Role::Assistant)
        .nth(keep - 1)?;
    let older = view.get(1..at)?;
    older
        .iter()
        .any(|message| message.role == Role::Assistant)
        .then_some(at)
}

/// The summary call: the view before `keep_from` under the summary prompt.
/// It offers the turn's tools with tool use disabled, because a provider
/// may refuse tool calls and results in a request that defines no tools.
pub(crate) fn summary_request(
    spec: &AgentSpec,
    compaction: &CompactionSpec,
    tools: Vec<ToolDefinition>,
    messages: Vec<Message>,
) -> ProviderRequest {
    let mut request = spec.request(tools, messages);
    let prompt = compaction.prompt.as_deref().unwrap_or(DEFAULT_PROMPT);
    request.system = vec![SystemBlock {
        text: prompt.to_owned(),
        cacheable: false,
    }];
    request.tool_choice = Some(ToolChoice::Disabled {});
    request.max_output_tokens = Some(compaction.max_output_tokens);
    request
}

/// The user message that stands in for the folded turns.
pub(crate) fn summary_message(text: &str) -> Message {
    Message::user_text(format!("{SUMMARY_HEADER}{text}"))
}

/// The compacted view: the first message as it was, the summary, then the
/// messages from `keep_from` on.
pub(crate) fn replacement(
    view: &[Arc<Message>],
    summary: Message,
    keep_from: usize,
) -> Vec<Arc<Message>> {
    let mut messages = Vec::with_capacity(2 + view.len().saturating_sub(keep_from));
    messages.extend(view.first().cloned());
    messages.push(Arc::new(summary));
    messages.extend(view.iter().skip(keep_from).cloned());
    messages
}

#[cfg(test)]
mod tests;
