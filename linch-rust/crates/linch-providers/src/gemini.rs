//! Google's Gemini API (`models.streamGenerateContent`, streamed as SSE).
//!
//! Wire rules, checked against the Gemini API reference
//! (`aistudio.google.com/api-reference/generate-content`) and the Vertex
//! AI generateContent guides on 2026-11-06; not yet run against a live key:
//! - `POST {base}/v1beta/models/{model}:streamGenerateContent?alt=sse`
//!   with `x-goog-api-key`, the model percent-encoded as one path segment.
//!   The stream has no end marker: it closes after the chunk with
//!   `finishReason`.
//! - Function calls may carry no `id`; the adapter then makes one that is
//!   random per response and never sends it back. Responses to parallel
//!   calls go in one turn, in call order.
//! - Thought signatures sit on parts (the first `functionCall` of a step,
//!   or a trailing, possibly empty, text part) and must return inside the
//!   same part. A turn with a signature or a call id is therefore kept raw
//!   in the message's `provider_metadata` and replayed verbatim.
//! - `thinkingBudget` is only sent when the request names one; Gemini 3
//!   and later prefer `thinkingLevel`, which effort maps to, and the two
//!   cannot be combined.
//! - Usage: `promptTokenCount` includes cached tokens and
//!   `candidatesTokenCount` excludes thoughts.
//! - Caching is implicit on Gemini, so `cache_control` is ignored.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{Provider, ProviderError, ProviderRequest, ProviderStream};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;

mod http;
mod payload;
mod stream;

/// The Gemini API root.
pub const DEFAULT_BASE_URL: &str = "https://generativelanguage.googleapis.com";

/// The environment variable [`GeminiOptions::from_env`] reads.
pub const API_KEY_ENV: &str = "GEMINI_API_KEY";

/// The `provider_metadata` key holding a Gemini turn's raw parts.
pub const PARTS_KEY: &str = "gemini_parts";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const READ_TIMEOUT: Duration = Duration::from_secs(300);

/// Everything but RFC 3986 unreserved characters is encoded, so a model
/// name stays one path segment: `/`, `?`, `#`, `:` and `%` cannot change
/// the path, the query or the method.
const PATH_SEGMENT: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'.')
    .remove(b'_')
    .remove(b'~');

/// Credentials and endpoint for [`Gemini`].
///
/// The model is not an option: it comes from each [`ProviderRequest`].
#[derive(Debug, Clone)]
pub struct GeminiOptions {
    /// The API key, sent as `x-goog-api-key`.
    pub api_key: ApiKey,
    /// API root without the `/v1beta/models/...` path.
    pub base_url: String,
}

impl GeminiOptions {
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

/// The Gemini provider.
#[derive(Debug, Clone)]
pub struct Gemini {
    client: reqwest::Client,
    options: GeminiOptions,
}

impl Gemini {
    /// Build a provider with its own HTTP client.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built,
    /// for example when no TLS roots are available.
    pub fn new(options: GeminiOptions) -> Result<Self, ProviderError> {
        let client = crate::transport::client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(&options.api_key, &err))?;
        Ok(Self { client, options })
    }
}

#[async_trait]
impl Provider for Gemini {
    fn id(&self) -> &str {
        "gemini"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = payload::build(&request)?;
        let model = request.model.trim_start_matches("models/");
        let url = format!(
            "{}/v1beta/models/{}:streamGenerateContent?alt=sse",
            self.options.base_url.trim_end_matches('/'),
            utf8_percent_encode(model, PATH_SEGMENT),
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
