//! [`StreamError`]: the ways a provider event stream can violate the
//! contract documented on [`crate::provider::Assembler`].

use crate::run::StopReason;
use crate::usage::Usage;

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
        /// Usage observed on the `message_end` event that triggered this
        /// error, preserved so the caller's spend accounting is never lost
        /// just because the stream also broke a shape rule.
        usage: Usage,
        /// Stop reason observed on the `message_end` event that triggered
        /// this error.
        stop_reason: StopReason,
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
    ToolUseWithoutCalls {
        /// Usage observed on the `message_end` event that triggered this
        /// error, preserved so the caller's spend accounting is never lost
        /// just because the stream also broke a shape rule.
        usage: Usage,
        /// Stop reason observed on the `message_end` event that triggered
        /// this error (always [`StopReason::ToolUse`]).
        stop_reason: StopReason,
    },
    /// One or more tool calls completed but `stop_reason` was not `tool_use`.
    #[error("completed tool calls but stop_reason was {stop_reason:?}, not tool_use")]
    CallsWithoutToolUse {
        /// The stop reason actually reported.
        stop_reason: StopReason,
        /// Usage observed on the `message_end` event that triggered this
        /// error, preserved so the caller's spend accounting is never lost
        /// just because the stream also broke a shape rule.
        usage: Usage,
    },
}
