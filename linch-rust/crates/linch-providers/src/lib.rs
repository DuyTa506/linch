//! HTTP model provider adapters for Linch.
//!
//! Each adapter implements [`linch_core::Provider`] and translates its vendor
//! wire format into [`linch_core::ProviderEvent`]s. Adapters never retry on
//! their own and never put credentials into errors or logs.

#![forbid(unsafe_code)]
#![deny(
    clippy::todo,
    clippy::unimplemented,
    clippy::unwrap_used,
    clippy::panic,
    clippy::expect_used
)]

pub mod anthropic;
mod api_key;
mod base_url;
#[cfg(test)]
mod capabilities_tests;
mod clients;
mod context;
#[cfg(test)]
mod cut_stream_tests;
pub mod deepseek;
#[cfg(test)]
mod error_text_tests;
pub mod gemini;
mod models;
mod native;
pub mod openai;
mod openai_compat;
mod overflow;
mod pricing;
mod request;
pub mod responses;
pub mod servers;
mod sse;
#[cfg(test)]
mod test_server;
mod transport;
#[cfg(test)]
mod wire_tests;

pub use anthropic::{Anthropic, AnthropicOptions};
pub use api_key::ApiKey;
pub use base_url::{BaseUrlError, check_base_url};
pub use clients::HttpClients;
pub use deepseek::{DeepSeek, DeepSeekOptions};
pub use gemini::{Gemini, GeminiOptions};
pub use openai::{OpenAiChat, OpenAiChatOptions};
pub use pricing::{ModelPricing, cost, model_pricing};
pub use request::{CacheSalt, ChatOptions, RequestOptions, is_key_header};
pub use responses::{OpenAiResponses, ReasoningSummary, ResponsesOptions};
pub use servers::{Server, ServerOptions};
