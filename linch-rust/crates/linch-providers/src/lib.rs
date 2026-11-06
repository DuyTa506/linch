//! HTTP model provider adapters for Linch.
//!
//! Each adapter implements [`linch_core::Provider`] and translates its vendor
//! wire format into [`linch_core::ProviderEvent`]s. Adapters never retry on
//! their own and never put credentials into errors or logs.

#![forbid(unsafe_code)]

pub mod anthropic;
mod api_key;
pub mod deepseek;
pub mod gemini;
pub mod openai;
mod openai_compat;
pub mod responses;
pub mod servers;
mod sse;
#[cfg(test)]
mod test_server;
mod transport;

pub use anthropic::{Anthropic, AnthropicOptions};
pub use api_key::ApiKey;
pub use deepseek::{DeepSeek, DeepSeekOptions};
pub use gemini::{Gemini, GeminiOptions};
pub use openai::{OpenAiChat, OpenAiChatOptions};
pub use responses::{OpenAiResponses, ReasoningSummary, ResponsesOptions};
pub use servers::{Server, ServerOptions};
