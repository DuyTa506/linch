//! Explicit profiles for OpenAI-compatible inference servers.
//!
//! Each [`Server`] runs on [`crate::OpenAiChat`] with its own profile,
//! which states what that server supports: the reasoning field, the
//! `tool_choice` forms, usage in the stream, the auth header and how
//! thinking is turned off. A profile copies nothing from OpenAI's; the
//! findings behind each one are in its module's docs.

use linch_core::{JsonObject, Usage};

use crate::ApiKey;
use crate::openai::ChatProfile;
use crate::openai_compat::stream::WireUsage;

mod llamacpp;
mod sglang;
mod vllm;

/// An OpenAI-compatible server with a declared profile.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Server {
    /// vLLM's OpenAI-compatible server (`vllm serve`).
    Vllm,
    /// SGLang's OpenAI-compatible server (`sglang.launch_server`).
    SgLang,
    /// The llama.cpp server (`llama-server`).
    LlamaCpp,
}

impl Server {
    pub(crate) fn profile(self) -> &'static ChatProfile {
        match self {
            Self::Vllm => &vllm::PROFILE,
            Self::SgLang => &sglang::PROFILE,
            Self::LlamaCpp => &llamacpp::PROFILE,
        }
    }

    /// The `chat_template_kwargs` this server's docs give for turning
    /// thinking off.
    fn thinking_off_kwargs(self) -> JsonObject {
        match self {
            Self::Vllm => vllm::thinking_off_kwargs(),
            Self::SgLang => sglang::thinking_off_kwargs(),
            Self::LlamaCpp => llamacpp::thinking_off_kwargs(),
        }
    }
}

/// Endpoint, credentials and thinking switch for one server.
///
/// The model is not an option: it comes from each request.
#[derive(Debug, Clone)]
pub struct ServerOptions {
    /// Which server this is.
    pub server: Server,
    /// API root including `/v1`, for example `http://localhost:8000/v1`.
    pub base_url: String,
    /// The key the server was started with (`--api-key`), sent as
    /// `Authorization: Bearer`. `None` sends no auth header.
    pub api_key: Option<ApiKey>,
    /// `chat_template_kwargs` sent when a request asks for
    /// [`linch_core::provider::Thinking::Disabled`]. The switch is defined
    /// by the model's chat template; an empty object sends nothing.
    pub thinking_off_kwargs: JsonObject,
}

impl ServerOptions {
    /// Options for `server` at `base_url`, with no key and the thinking
    /// switch from that server's docs.
    pub fn new(server: Server, base_url: impl Into<String>) -> Self {
        Self {
            server,
            base_url: base_url.into(),
            api_key: None,
            thinking_off_kwargs: server.thinking_off_kwargs(),
        }
    }
}

/// `prompt_tokens` includes the cached prefix, reported as
/// `prompt_tokens_details.cached_tokens`; Linch counts it apart.
fn prompt_minus_cached(wire: &WireUsage) -> Usage {
    let cached = wire
        .prompt_tokens_details
        .as_ref()
        .and_then(|details| details.cached_tokens)
        .unwrap_or(0);
    Usage {
        input_tokens: wire.prompt_tokens.saturating_sub(cached),
        output_tokens: wire.completion_tokens,
        cache_read_tokens: cached,
        cache_creation_tokens: 0,
    }
}

fn kwargs(value: serde_json::Value) -> JsonObject {
    value.as_object().cloned().unwrap_or_default()
}

#[cfg(test)]
mod tests;
