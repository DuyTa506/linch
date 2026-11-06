//! OpenAI's Responses API (`POST /v1/responses`, streamed).
//!
//! Wire rules, checked against developers.openai.com on 2026-11-06
//! (API reference for `responses/create` and its streaming events, and the
//! reasoning, function-calling and conversation-state guides):
//! - System blocks become `instructions`; the conversation is `input`.
//! - Function tools are sent with `strict: false`: omitting it lets the
//!   API rewrite a schema into strict mode.
//! - Each response's `output` items, reasoning items included, are kept in
//!   the assistant message's `provider_metadata` and sent back verbatim, as
//!   the docs ask for reasoning items with tool calls. No
//!   `previous_response_id` is sent, so every request carries its full
//!   history.
//! - With [`ResponsesOptions::store`] off, `store: false` is sent and
//!   reasoning comes back encrypted, so replay needs no server state.
//! - There is no stop-sequence parameter, so stop sequences are rejected.
//! - `input_tokens` includes cache reads and writes; usage splits them out.
//! - The stream ends at `response.completed`, `response.incomplete` or
//!   `response.failed`, with no `[DONE]`.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{Provider, ProviderError, ProviderRequest, ProviderStream};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::openai::{API_KEY_ENV, DEFAULT_BASE_URL};
use crate::openai_compat::http;

mod catalog;
mod payload;
mod stream;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// Reasoning models can think for minutes before the first event.
const READ_TIMEOUT: Duration = Duration::from_secs(600);

/// How much of the model's reasoning to summarize back as thinking text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReasoningSummary {
    /// The most detailed summary the model supports.
    Auto,
    /// A short summary.
    Concise,
    /// A detailed summary.
    Detailed,
}

impl ReasoningSummary {
    fn wire(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Concise => "concise",
            Self::Detailed => "detailed",
        }
    }
}

/// Credentials, endpoint and reasoning settings for [`OpenAiResponses`].
///
/// The model is not an option: it comes from each [`ProviderRequest`].
#[derive(Debug, Clone)]
pub struct ResponsesOptions {
    /// The API key, sent as a bearer token.
    pub api_key: ApiKey,
    /// API root without the `/responses` path.
    pub base_url: String,
    /// Reasoning summary to ask for; `None` asks for none, so no thinking
    /// text streams.
    pub reasoning_summary: Option<ReasoningSummary>,
    /// Whether OpenAI stores each response (`store`, on by default). Off
    /// sends `store: false` and asks for encrypted reasoning, which zero
    /// data retention organizations need.
    pub store: bool,
    /// Whether a tool added or removed mid-session goes in the input as
    /// `additional_tools`, with an `allowed_tools` choice, keeping the
    /// prompt cache, instead of in `tools`. Used only with gpt-5.4 and
    /// later; off by default and not verified against the live API.
    pub native_catalog_changes: bool,
}

impl ResponsesOptions {
    /// Options for the public endpoint with `api_key`, storing responses
    /// and asking for no reasoning summary.
    pub fn new(api_key: ApiKey) -> Self {
        Self {
            api_key,
            base_url: DEFAULT_BASE_URL.into(),
            reasoning_summary: None,
            store: true,
            native_catalog_changes: false,
        }
    }

    /// Options with the key from [`API_KEY_ENV`], or `None` when it is unset
    /// or empty.
    pub fn from_env() -> Option<Self> {
        ApiKey::from_env(API_KEY_ENV).map(Self::new)
    }
}

/// The OpenAI Responses provider.
#[derive(Debug, Clone)]
pub struct OpenAiResponses {
    client: reqwest::Client,
    options: ResponsesOptions,
}

impl OpenAiResponses {
    /// Build a provider with its own HTTP client.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built.
    pub fn new(options: ResponsesOptions) -> Result<Self, ProviderError> {
        let client = crate::transport::client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(Some(&options.api_key), &err))?;
        Ok(Self { client, options })
    }
}

#[async_trait]
impl Provider for OpenAiResponses {
    fn id(&self) -> &str {
        "openai-responses"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = payload::build_payload(&request, &self.options)?;
        let url = format!("{}/responses", self.options.base_url.trim_end_matches('/'));
        let key = &self.options.api_key;
        let response = http::post(&self.client, &url, Some(key), &body, &cancel).await?;
        Ok(stream::events(
            response.bytes_stream(),
            stream::Translator::new(request.model),
            cancel,
            key.clone(),
        ))
    }
}

#[cfg(test)]
mod tests;
