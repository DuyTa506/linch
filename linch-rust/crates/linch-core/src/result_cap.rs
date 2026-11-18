//! The tool result cap: one bound on what a tool result puts in the
//! model's view, with a tool to read the rest.
//!
//! A tool that returns more than [`ToolResultCap::max_tokens`] (estimated,
//! four bytes per token as [`crate::compaction`] counts) puts only its head
//! and its tail in the provider view, around a marker that says how much is
//! left out and how to read it. The whole result stays where it was: the
//! `tool_call_end` event, the `user` event, the run's checkpoint and the
//! session's `full_history` hold every byte, and so do the loop guard's and
//! the tool cache's keys. Only the view is cut, as a logged fact: the
//! results message is appended as two entries, the whole one for the audit
//! projection only and the capped one for the provider view only
//! (`Runner::append_results`), so a reload rebuilds the same
//! view and the log shows exactly what the model was sent.
//!
//! The cut is a pure function of the result and the spec, so a crash and
//! resume cut the same bytes. A result at or under the cap is appended as it
//! always was: no copy, no extra entry.
//!
//! With the cap on, the agent also has the `ReadToolResult` tool
//! ([`reader::ReadToolResultTool`]), which returns any slice of a result
//! the session logged, in the same bounded size, so a slice never needs a
//! cap of its own.
//!
//! The cut is for results of any tool, host tools included. A tool that caps
//! itself (`Bash`, `Grep`, `Glob`, the MCP tools, `memory_recall`) keeps its
//! own cap, which runs first: its result reaches this cap only if it is
//! still over it.

pub(crate) mod reader;

use serde::{Deserialize, Serialize};

use crate::compaction::{BYTES_PER_TOKEN, head_tail};
use crate::message::{ContentBlock, Message, TextBlock, ToolResultBlock, ToolResultPart};

/// The tool that reads a result the cap cut.
pub const READ_TOOL: &str = "ReadToolResult";

/// Least `max_tokens`: the marker alone takes about 120 bytes, and the
/// reader's slices (and their footers) must fit under the cap again.
const MIN_TOKENS: u64 = 128;

fn default_head_fraction() -> f64 {
    0.3
}

/// A bound on the tokens one tool result puts in the model's view. Off
/// unless an [`AgentSpec`](crate::AgentSpec) sets one.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolResultCap {
    /// Most estimated tokens (four bytes per token, as compaction counts)
    /// of one tool result's text in the view, the marker included; at least
    /// 128. The estimate counts 20% to 2x fewer tokens than a provider
    /// does, so leave room for that.
    pub max_tokens: u64,
    /// The share of the kept bytes that is the head, from 0 to 1 (default
    /// 0.3): the tail is kept for what ends a command's output, an error
    /// or a summary line.
    #[serde(default = "default_head_fraction")]
    pub head_fraction: f64,
}

impl ToolResultCap {
    /// A cap at `max_tokens`, with the default head share.
    pub fn new(max_tokens: u64) -> Self {
        Self {
            max_tokens,
            head_fraction: default_head_fraction(),
        }
    }

    /// Why this cap cannot run, if it cannot.
    pub(crate) fn problem(&self) -> Option<String> {
        if self.max_tokens < MIN_TOKENS {
            return Some(format!(
                "tool_result_cap.max_tokens must be at least {MIN_TOKENS}"
            ));
        }
        if !(0.0..=1.0).contains(&self.head_fraction) {
            return Some("tool_result_cap.head_fraction must be a number from 0 to 1".into());
        }
        None
    }

    /// The most bytes of one result's text the view keeps, the marker
    /// included.
    pub(crate) fn budget(&self) -> usize {
        let bytes = self.max_tokens.saturating_mul(BYTES_PER_TOKEN);
        usize::try_from(bytes).unwrap_or(usize::MAX)
    }

    /// `message` with each over-cap tool result cut; `None` when none is
    /// over, which costs a length check per text part and no allocation.
    pub(crate) fn apply(&self, message: &Message) -> Option<Message> {
        let budget = self.budget();
        let over = |block: &ContentBlock| match block {
            ContentBlock::ToolResult(result) => text_bytes(result) > budget,
            _ => false,
        };
        if !message.content.iter().any(over) {
            return None;
        }
        let mut capped = message.clone();
        for block in &mut capped.content {
            if let ContentBlock::ToolResult(result) = block
                && text_bytes(result) > budget
            {
                self.cut_result(result, budget);
            }
        }
        Some(capped)
    }

    /// Replace the text parts of `result` by one text part holding the
    /// head and the tail of their joined text. Images stay.
    fn cut_result(&self, result: &mut ToolResultBlock, budget: usize) {
        let text = joined_text(result);
        let id = &result.tool_use_id;
        let marker = |omitted: usize, head: usize| marker(id, omitted, head);
        // The marker is sized for the largest numbers it could hold, so
        // the cut text never goes over the budget.
        let room = budget.saturating_sub(marker(text.len(), text.len()).len());
        let head = (room as f64 * self.head_fraction) as usize;
        let cut = head_tail(&text, head, room - head, marker);
        let mut content = Vec::with_capacity(result.content.len());
        let mut cut = Some(cut);
        for part in std::mem::take(&mut result.content) {
            match part {
                ToolResultPart::Text(_) => {
                    if let Some(text) = cut.take() {
                        content.push(ToolResultPart::Text(TextBlock { text }));
                    }
                }
                image => content.push(image),
            }
        }
        result.content = content;
    }
}

/// What the view says where `omitted` bytes of a result were cut, after its
/// first `head` bytes.
fn marker(tool_use_id: &str, omitted: usize, head: usize) -> String {
    format!(
        "\n[... {omitted} bytes omitted; read them with {READ_TOOL}(tool_use_id=\"{tool_use_id}\", \
         offset={head}) ...]\n"
    )
}

/// Bytes of the text parts of `result`.
fn text_bytes(result: &ToolResultBlock) -> usize {
    (result.content.iter())
        .map(|part| match part {
            ToolResultPart::Text(text) => text.text.len(),
            ToolResultPart::Image(_) => 0,
        })
        .sum()
}

/// The text of `result`: its text parts, in order, joined by a newline.
/// Offsets of [`READ_TOOL`] index this text.
pub(crate) fn joined_text(result: &ToolResultBlock) -> String {
    let parts = result.content.iter().filter_map(|part| match part {
        ToolResultPart::Text(text) => Some(text.text.as_str()),
        ToolResultPart::Image(_) => None,
    });
    parts.collect::<Vec<_>>().join("\n")
}

#[cfg(test)]
mod tests;
