//! HTTP model provider adapters for Linch.
//!
//! Each adapter implements [`linch_core::Provider`] and translates its vendor
//! wire format into [`linch_core::ProviderEvent`]s. Adapters never retry on
//! their own and never put credentials into errors or logs.

#![forbid(unsafe_code)]

mod api_key;
pub mod deepseek;
mod openai_compat;

pub use api_key::ApiKey;
pub use deepseek::{DeepSeek, DeepSeekOptions};
