//! Python-free core of the Linch agent kernel: shared types, wire codecs, the
//! event vocabulary, and the append-only session log.
//!
//! The wire format is JSON. Every type rejects unknown fields and unknown
//! `type` tags when decoding, so a reader never silently drops data. The one
//! exception is an event its writer marked ignorable, which is kept as raw
//! JSON in [`IgnorableEvent`].

#![forbid(unsafe_code)]

pub mod canonical;
pub mod events;
pub mod ids;
pub mod json;
pub mod message;
pub mod provider;
pub mod run;
pub mod session_log;
pub mod usage;

pub use canonical::{CanonicalError, Digest, digest, digest_with_domain, to_canonical_string};
pub use events::{
    Envelope, Event, EventError, IgnorableEvent, Persistence, SubagentEvent, WIRE_VERSION,
};
pub use ids::{RunId, SessionId};
pub use json::{JsonObject, JsonValue};
pub use message::{
    CatalogChangeBlock, ContentBlock, ImageBlock, ImageSource, Message, RedactedThinkingBlock,
    Role, SystemBlock, TextBlock, ThinkingBlock, ToolResultBlock, ToolResultPart, ToolUseBlock,
};
pub use provider::{
    Assembler, AssistantAssembly, CacheControl, CacheTtl, OutputSchema, ProviderEvent,
    ProviderRequest, ReasoningEffort, StreamError, ToolChoice, ToolDefinition, assemble,
};
pub use run::{RunStatus, StopReason};
pub use session_log::{LogEntry, MessageEntry, ProjectionEntry, SessionLog};
pub use usage::Usage;
