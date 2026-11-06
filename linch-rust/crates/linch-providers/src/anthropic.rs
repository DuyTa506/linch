//! Anthropic's Messages API (`POST /v1/messages`, streamed as SSE).
//!
//! Wire rules, checked against `platform.claude.com/docs` on 2026-11-06:
//! - Auth is `x-api-key`; `anthropic-version` is pinned to [`API_VERSION`].
//!   `2026-10-21` removes `temperature`, which [`ProviderRequest`] carries,
//!   so the adapter stays on `2023-06-01` until that field is dropped.
//! - `max_tokens` is required; [`DEFAULT_MAX_TOKENS`] fills it.
//! - Thinking: `enabled` needs `budget_tokens` below `max_tokens` and
//!   rejects a forced `tool_choice`; an unbudgeted request is `adaptive`.
//!   Every thinking block is sent back with its signature, unmodified.
//! - `cache_control` is `{"type": "ephemeral"}` with `"ttl": "1h"` for
//!   the long lifetime, on the last cacheable system block and the last
//!   block of the last user message.
//! - `message_delta.usage` is cumulative and may restate input and cache
//!   counts; a present field replaces the `message_start` value.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{Provider, ProviderError, ProviderRequest, ProviderStream};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;

mod http;
mod payload;
mod stream;

/// The Claude API root.
pub const DEFAULT_BASE_URL: &str = "https://api.anthropic.com";

/// The environment variable [`AnthropicOptions::from_env`] reads the key from.
pub const API_KEY_ENV: &str = "ANTHROPIC_API_KEY";

/// The environment variable that overrides [`DEFAULT_BASE_URL`] in
/// [`AnthropicOptions::from_env`], as in Anthropic's SDKs.
pub const BASE_URL_ENV: &str = "ANTHROPIC_BASE_URL";

/// The `anthropic-version` header sent on every request.
pub const API_VERSION: &str = "2023-06-01";

/// `max_tokens` when the request sets no output limit.
pub const DEFAULT_MAX_TOKENS: u32 = 8192;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// The API sends `ping` events while it works, so a long silence means a
/// dead connection.
const READ_TIMEOUT: Duration = Duration::from_secs(300);

/// Credentials and endpoint for [`Anthropic`].
///
/// The model is not an option: it comes from each [`ProviderRequest`].
#[derive(Debug, Clone)]
pub struct AnthropicOptions {
    /// The API key, sent as `x-api-key`.
    pub api_key: ApiKey,
    /// API root without the `/v1/messages` path. Any server that speaks
    /// the Messages API works here.
    pub base_url: String,
}

impl AnthropicOptions {
    /// Options for the public endpoint with `api_key`.
    pub fn new(api_key: ApiKey) -> Self {
        Self {
            api_key,
            base_url: DEFAULT_BASE_URL.into(),
        }
    }

    /// Options with the key from [`API_KEY_ENV`] and the root from
    /// [`BASE_URL_ENV`] when it is set; `None` when the key is unset or
    /// empty.
    pub fn from_env() -> Option<Self> {
        let mut options = Self::new(ApiKey::from_env(API_KEY_ENV)?);
        if let Some(base_url) = std::env::var(BASE_URL_ENV)
            .ok()
            .filter(|url| !url.trim().is_empty())
        {
            options.base_url = base_url.trim().into();
        }
        Some(options)
    }
}

/// The Anthropic Messages provider.
#[derive(Debug, Clone)]
pub struct Anthropic {
    client: reqwest::Client,
    options: AnthropicOptions,
}

impl Anthropic {
    /// Build a provider with its own HTTP client.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built,
    /// for example when no TLS roots are available.
    pub fn new(options: AnthropicOptions) -> Result<Self, ProviderError> {
        let client = reqwest::Client::builder()
            .connect_timeout(CONNECT_TIMEOUT)
            .read_timeout(READ_TIMEOUT)
            .build()
            .map_err(|err| http::transport(&options.api_key, &err))?;
        Ok(Self { client, options })
    }
}

#[async_trait]
impl Provider for Anthropic {
    fn id(&self) -> &str {
        "anthropic"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = payload::build(&request)?;
        let url = format!(
            "{}/v1/messages",
            self.options.base_url.trim_end_matches('/')
        );
        let key = &self.options.api_key;
        let response = http::post(&self.client, &url, key, &body, &cancel).await?;
        let translator = stream::Translator::new(request.model, key.clone());
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            key.clone(),
        ))
    }
}

#[cfg(test)]
mod tests;
