//! Mechanics shared by OpenAI-compatible Chat Completions adapters.
//!
//! Nothing here is a default an adapter inherits implicitly: each adapter
//! builds its own payload from these pieces and passes its own usage
//! normalizer, after checking the pieces against its server's docs.
//!
//! - [`payload`]: Linch messages and tools to chat-completions JSON.
//! - [`stream`]: SSE `data:` chunks to [`linch_core::ProviderEvent`]s.
//! - [`http`]: the POST, status-to-[`linch_core::ProviderError`] mapping
//!   and credential redaction.

pub(crate) mod http;
pub(crate) mod payload;
pub(crate) mod stream;
