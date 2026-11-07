//! The provider stream contract: the request a provider receives, the
//! normalized event stream it emits, and the [`Assembler`] that turns that
//! stream into one assistant turn.
//!
//! An adapter never emits raw vendor SDK objects or a vendor-specific shape;
//! it translates into [`ProviderEvent`], and everything downstream of that
//! boundary — the loop, the session log, the run store — only ever sees the
//! resulting [`AssistantAssembly`]. See
//! `linch-rust/docs/architecture/providers.md` for the full contract this
//! module enforces.

mod assembler;
pub mod call;
mod client;
mod event;
mod request;
pub mod scripted;

pub use assembler::{Assembler, AssistantAssembly, StreamError, assemble};
pub use client::{DEFAULT_CONTEXT_WINDOW, Provider, ProviderError, ProviderStream};
pub use event::{
    MessageEndEvent, MessageStartEvent, ProviderEvent, TextDeltaEvent, ThinkingDeltaEvent,
    ToolUseEndEvent, ToolUseInputDeltaEvent, ToolUseStartEvent,
};
pub use request::{
    CacheControl, CacheTtl, OutputSchema, ProviderRequest, ReasoningEffort, Thinking,
    ThinkingDisplay, ToolChoice, ToolDefinition,
};
