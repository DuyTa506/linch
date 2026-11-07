//! The Responses stream event shapes the translator reads.
//!
//! Events are read leniently: unknown event types and fields (such as
//! `sequence_number` and `obfuscation`) are ignored, and nulls read as
//! absent.

use linch_core::{JsonValue, Usage};
use serde::Deserialize;

#[derive(Deserialize)]
pub(super) struct WireEvent {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub delta: Option<String>,
    #[serde(default)]
    pub item_id: Option<String>,
    #[serde(default)]
    pub summary_index: Option<u32>,
    #[serde(default)]
    pub item: Option<Item>,
    #[serde(default)]
    pub response: Option<WireResponse>,
    #[serde(default)]
    pub code: Option<JsonValue>,
    #[serde(default)]
    pub message: Option<String>,
    /// The nested shape some error frames use instead of top-level fields.
    #[serde(default)]
    pub error: Option<WireError>,
}

#[derive(Deserialize)]
pub(super) struct Item {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub call_id: Option<String>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub arguments: Option<String>,
}

#[derive(Default, Deserialize)]
pub(super) struct WireResponse {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub incomplete_details: Option<IncompleteDetails>,
    #[serde(default)]
    pub error: Option<WireError>,
    #[serde(default)]
    pub usage: Option<WireUsage>,
    #[serde(default)]
    pub output: Option<Vec<JsonValue>>,
}

#[derive(Deserialize)]
pub(super) struct IncompleteDetails {
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Default, Deserialize)]
pub(super) struct WireError {
    /// The error type, which the nested `error` frame shape carries.
    #[serde(default, rename = "type")]
    pub kind: Option<String>,
    #[serde(default)]
    pub code: Option<JsonValue>,
    #[serde(default)]
    pub message: Option<String>,
}

#[derive(Default, Deserialize)]
pub(super) struct WireUsage {
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
    #[serde(default)]
    pub input_tokens_details: Option<InputDetails>,
}

#[derive(Default, Deserialize)]
pub(super) struct InputDetails {
    #[serde(default)]
    pub cached_tokens: Option<u64>,
    #[serde(default)]
    pub cache_write_tokens: Option<u64>,
}

/// `input_tokens` includes cache reads and cache writes; Linch counts them
/// apart. `output_tokens` already includes reasoning tokens.
pub(super) fn usage(wire: WireUsage) -> Usage {
    let details = wire.input_tokens_details.unwrap_or_default();
    let read = details.cached_tokens.unwrap_or(0);
    let written = details.cache_write_tokens.unwrap_or(0);
    Usage {
        input_tokens: wire
            .input_tokens
            .saturating_sub(read)
            .saturating_sub(written),
        output_tokens: wire.output_tokens,
        cache_read_tokens: read,
        cache_creation_tokens: written,
    }
}
