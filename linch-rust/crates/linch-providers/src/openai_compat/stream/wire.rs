//! The Chat Completions chunk shapes the translator reads.
//!
//! Vendor chunks are read leniently: unknown fields (`system_fingerprint`,
//! `logprobs`, `obfuscation`, new usage buckets) are ignored, and nulls
//! read as absent.

use linch_core::JsonValue;
use serde::Deserialize;

#[derive(Deserialize)]
pub(super) struct Chunk {
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub choices: Option<Vec<Choice>>,
    #[serde(default)]
    pub usage: Option<WireUsage>,
    /// An error frame sent after the stream started.
    #[serde(default)]
    pub error: Option<JsonValue>,
}

#[derive(Deserialize)]
pub(super) struct Choice {
    #[serde(default)]
    pub delta: Option<Delta>,
    #[serde(default)]
    pub finish_reason: Option<String>,
}

#[derive(Deserialize)]
pub(super) struct Delta {
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub reasoning_content: Option<String>,
    #[serde(default)]
    pub reasoning: Option<String>,
    #[serde(default)]
    pub tool_calls: Option<Vec<ToolCallDelta>>,
}

#[derive(Deserialize)]
pub(super) struct ToolCallDelta {
    pub index: u32,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub function: Option<FunctionDelta>,
}

#[derive(Deserialize)]
pub(super) struct FunctionDelta {
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub arguments: Option<String>,
}

/// The delta field a server streams reasoning text in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReasoningField {
    /// `delta.reasoning_content` (DeepSeek, SGLang, llama.cpp).
    ReasoningContent,
    /// `delta.reasoning` (current vLLM).
    Reasoning,
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
    /// OpenAI's cache writes, also counted in `prompt_tokens`.
    #[serde(default)]
    pub cache_write_tokens: Option<u64>,
    /// vLLM's prefix-cache writes, also counted in `prompt_tokens`.
    #[serde(default)]
    pub created_cache_tokens: Option<u64>,
}
