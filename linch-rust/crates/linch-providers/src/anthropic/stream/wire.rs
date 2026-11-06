//! The Messages SSE payloads, read leniently.

use linch_core::JsonValue;
use serde::Deserialize;

// Vendor events are read leniently: unknown fields are ignored, and unknown
// event, block and delta types are skipped, as the API's versioning policy
// asks (new types may appear at any time).

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum Wire {
    MessageStart {
        message: WireMessage,
    },
    ContentBlockStart {
        index: u64,
        content_block: WireBlock,
    },
    ContentBlockDelta {
        index: u64,
        delta: WireDelta,
    },
    ContentBlockStop {
        index: u64,
    },
    MessageDelta {
        delta: WireMessageDelta,
        #[serde(default)]
        usage: Option<WireUsage>,
    },
    MessageStop,
    Error {
        error: JsonValue,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
pub(super) struct WireMessage {
    #[serde(default)]
    pub(super) model: Option<String>,
    #[serde(default)]
    pub(super) usage: Option<WireUsage>,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum WireBlock {
    Text {
        #[serde(default)]
        text: String,
    },
    Thinking {
        #[serde(default)]
        thinking: String,
        #[serde(default)]
        signature: String,
    },
    RedactedThinking {
        data: String,
    },
    ToolUse {
        id: String,
        name: String,
        #[serde(default)]
        input: Option<JsonValue>,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum WireDelta {
    TextDelta {
        text: String,
    },
    ThinkingDelta {
        thinking: String,
    },
    SignatureDelta {
        signature: String,
    },
    InputJsonDelta {
        partial_json: String,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
pub(super) struct WireMessageDelta {
    #[serde(default)]
    pub(super) stop_reason: Option<String>,
}

/// Token counts; `message_delta` restates them cumulatively, so a present
/// field replaces the value from `message_start`.
#[derive(Deserialize)]
pub(super) struct WireUsage {
    #[serde(default)]
    pub(super) input_tokens: Option<u64>,
    #[serde(default)]
    pub(super) output_tokens: Option<u64>,
    #[serde(default)]
    pub(super) cache_read_input_tokens: Option<u64>,
    #[serde(default)]
    pub(super) cache_creation_input_tokens: Option<u64>,
}

/// A pre-generation error some compatible servers send as a bare
/// `{"error": {...}}` frame without a `type`.
#[derive(Deserialize)]
pub(super) struct BareError {
    pub(super) error: JsonValue,
}
