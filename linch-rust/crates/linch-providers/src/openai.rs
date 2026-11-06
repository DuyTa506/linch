//! OpenAI Chat Completions, and the OpenAI-compatible servers that speak it.
//!
//! One adapter, [`OpenAiChat`], serves OpenAI itself ([`OpenAiChat::new`])
//! and each [`Server`](crate::Server) ([`OpenAiChat::server`]). Every
//! target has its own profile that states each wire rule explicitly; a
//! server profile inherits nothing from OpenAI's.
//!
//! OpenAI rules, checked on 2026-11-06 against the Chat Completions
//! reference and streaming events at developers.openai.com, the OpenAPI
//! spec 2.3.0 and the reasoning guide:
//! - The output limit is `max_completion_tokens`; `max_tokens` is
//!   deprecated and not compatible with o-series models.
//! - `reasoning_effort` takes `none`, `minimal`, `low`, `medium`, `high`,
//!   `xhigh` or `max`, per model. Linch's levels map one to one onto the
//!   last five; `Thinking` is not sent. From GPT-5.4 on, tool calling on
//!   Chat Completions needs effort `none`; use the Responses adapter.
//! - The stream carries no reasoning text, so none is read or sent back.
//! - Every `tool_choice` form is accepted, the named one as
//!   `{"type": "function", "function": {"name": ...}}`.
//! - Usage comes on a last chunk with empty `choices` when
//!   `stream_options.include_usage` is set. `prompt_tokens` includes
//!   `cached_tokens` and `cache_write_tokens`; `completion_tokens` includes
//!   reasoning tokens.
//! - Later tool-call deltas carry explicit nulls; `obfuscation` is a
//!   top-level chunk field. Both are ignored.
//! - An error after the 200 arrives as a `data: {"error": {...}}` frame.
//! - Auth is `Authorization: Bearer <key>`, and the key is required.
//!   `Retry-After` is whole seconds.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{JsonObject, Provider, ProviderError, ProviderRequest, ProviderStream};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::openai_compat::stream::Translator;
use crate::openai_compat::{http, stream};
use crate::servers::ServerOptions;

mod profile;
#[cfg(test)]
pub(crate) mod test_support;
#[cfg(test)]
mod tests;

#[cfg(test)]
pub(crate) use profile::build_payload as profile_payload;
pub(crate) use profile::{ChatProfile, ToolChoiceSupport, effort_value};

/// OpenAI's API root, including the `/v1` prefix.
pub const DEFAULT_BASE_URL: &str = "https://api.openai.com/v1";

/// The environment variable [`OpenAiChatOptions::from_env`] reads.
pub const API_KEY_ENV: &str = "OPENAI_API_KEY";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// Reasoning models can think for minutes before the first token.
const READ_TIMEOUT: Duration = Duration::from_secs(600);

/// Credentials and endpoint for OpenAI's Chat Completions API.
///
/// The model is not an option: it comes from each [`ProviderRequest`].
#[derive(Debug, Clone)]
pub struct OpenAiChatOptions {
    /// The API key, sent as a bearer token.
    pub api_key: ApiKey,
    /// API root without the `/chat/completions` path.
    pub base_url: String,
}

impl OpenAiChatOptions {
    /// Options for the public endpoint with `api_key`.
    pub fn new(api_key: ApiKey) -> Self {
        Self {
            api_key,
            base_url: DEFAULT_BASE_URL.into(),
        }
    }

    /// Options with the key from [`API_KEY_ENV`], or `None` when it is unset
    /// or empty.
    pub fn from_env() -> Option<Self> {
        ApiKey::from_env(API_KEY_ENV).map(Self::new)
    }
}

/// A Chat Completions provider for OpenAI or one OpenAI-compatible server.
#[derive(Debug, Clone)]
pub struct OpenAiChat {
    client: reqwest::Client,
    profile: &'static ChatProfile,
    base_url: String,
    api_key: Option<ApiKey>,
    thinking_off: JsonObject,
}

impl OpenAiChat {
    /// A provider for OpenAI's own endpoint.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built.
    pub fn new(options: OpenAiChatOptions) -> Result<Self, ProviderError> {
        Self::build(
            &profile::OPENAI,
            options.base_url,
            Some(options.api_key),
            JsonObject::new(),
        )
    }

    /// A provider for [`ServerOptions::server`], using that server's
    /// declared profile.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built.
    pub fn server(options: ServerOptions) -> Result<Self, ProviderError> {
        Self::build(
            options.server.profile(),
            options.base_url,
            options.api_key,
            options.thinking_off_kwargs,
        )
    }

    fn build(
        profile: &'static ChatProfile,
        base_url: String,
        api_key: Option<ApiKey>,
        thinking_off: JsonObject,
    ) -> Result<Self, ProviderError> {
        let client = crate::transport::client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(api_key.as_ref(), &err))?;
        Ok(Self {
            client,
            profile,
            base_url,
            api_key,
            thinking_off,
        })
    }
}

#[async_trait]
impl Provider for OpenAiChat {
    fn id(&self) -> &str {
        self.profile.id
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = profile::build_payload(self.profile, &self.thinking_off, &request)?;
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let key = self.api_key.as_ref();
        let response = http::post(&self.client, &url, key, &body, &cancel).await?;
        let translator = Translator::new(
            request.model,
            self.profile.reasoning_field,
            self.profile.usage,
        );
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            self.api_key.clone(),
        ))
    }
}
