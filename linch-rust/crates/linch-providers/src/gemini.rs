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
//! - `temperature` goes in `generationConfig` and takes 0.0 to 2.0; checked
//!   2026-11-17, the reference says it "is silently ignored" on Gemini 3.6
//!   Flash and newer models.
//! - Usage: `promptTokenCount` includes cached tokens and
//!   `candidatesTokenCount` excludes thoughts.
//! - Caching is implicit on Gemini, so `cache_control` is ignored.
//! - Structured output (checked 2026-11-17 against the generateContent
//!   reference and the structured-output guide) is native:
//!   `generationConfig.responseMimeType` `application/json` with
//!   `responseJsonSchema` (JSON Schema; `responseSchema` is the older
//!   OpenAPI subset). Gemini 3 and later combine it with function
//!   calling; an older model refuses a request that has both.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    ContextSource, ModelList, ModelsError, OutputSchemaMode, PromptCache, Provider,
    ProviderCapabilities, ProviderError, ProviderRequest, ProviderStream, Support, ThinkingCaps,
    ToolChoiceCaps,
};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::request::ErrorScrub;

mod http;
pub(crate) mod payload;
mod stream;

/// The Gemini API root.
pub const DEFAULT_BASE_URL: &str = "https://generativelanguage.googleapis.com";

/// Python's `gemini._KNOWN_CONTEXT`, less the entries equal to
/// [`DEFAULT_CONTEXT_WINDOW`].
const KNOWN_CONTEXT: &[(&str, u64)] = &[("gemini-1.5-pro", 2_000_000)];

/// The window of a Gemini model the table does not name.
const DEFAULT_CONTEXT_WINDOW: u64 = 1_048_576;

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
    /// Extra body fields, headers and a timeout for every request.
    pub request: crate::RequestOptions,
}

impl GeminiOptions {
    /// Options for the public endpoint with `api_key`.
    pub fn new(api_key: ApiKey) -> Self {
        Self {
            api_key,
            base_url: DEFAULT_BASE_URL.into(),
            request: crate::RequestOptions::default(),
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
    client: crate::clients::SharedClient,
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
        crate::base_url::require(&options.base_url)?;
        let client = options
            .request
            .client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(&options.api_key, &err))?;
        Ok(Self { client, options })
    }
}

#[async_trait]
impl Provider for Gemini {
    fn id(&self) -> &str {
        "gemini"
    }

    /// Python's table: 2000000 for `gemini-1.5-pro`, else 1048576.
    fn context_window(&self, model: &str) -> u64 {
        crate::context::known(KNOWN_CONTEXT, model, DEFAULT_CONTEXT_WINDOW)
    }

    /// Gemini's own rules: native JSON schema output, a thinking budget or
    /// level, implicit caching, and a function call whose arguments arrive
    /// whole in one part.
    fn capabilities(&self, model: &str) -> ProviderCapabilities {
        let table = crate::context::in_table(KNOWN_CONTEXT, model);
        ProviderCapabilities {
            provider: self.id().to_owned(),
            model: model.to_owned(),
            declared: true,
            context_window: table.unwrap_or(DEFAULT_CONTEXT_WINDOW),
            context_source: if table.is_some() {
                ContextSource::Table
            } else {
                ContextSource::Default
            },
            output_schema: OutputSchemaMode::Native,
            tool_choice: ToolChoiceCaps::probe(|choice, thinking| {
                self.tool_choice_refusal(choice, thinking)
            }),
            thinking: ThinkingCaps {
                disable: true,
                budget: true,
                effort: true,
                reasoning_text: true,
            },
            vision: Support::Supported,
            streams_tool_arguments: false,
            prompt_cache: PromptCache::Automatic,
            stop_sequences: true,
        }
    }

    /// `GET /v1beta/models`, paged by `pageToken` while `nextPageToken`.
    async fn list_models(&self) -> Result<ModelList, ModelsError> {
        let base = self.options.base_url.trim_end_matches('/');
        let request = &self.options.request;
        let key = &self.options.api_key;
        let headers: Vec<(&str, &str)> = (request.key_to_send(key))
            .map(|key| ("x-goog-api-key", key.expose()))
            .into_iter()
            .collect();
        let mut models = Vec::new();
        let mut token: Option<String> = None;
        let mut truncated = false;
        for n in 0..crate::models::MAX_PAGES {
            let mut url = format!("{base}/v1beta/models?pageSize=1000");
            if let Some(token) = &token {
                url.push_str("&pageToken=");
                url.extend(utf8_percent_encode(token, PATH_SEGMENT));
            }
            let page = (crate::models::get_json(&self.client, &url, &headers, Some(key)).await)
                .map_err(|err| request.scrub_error(err))?;
            let (listed, next) = crate::models::gemini_page(&page);
            models.extend(listed);
            match next {
                Some(next) => token = Some(next),
                None => break,
            }
            truncated = n + 1 == crate::models::MAX_PAGES;
        }
        Ok(ModelList { models, truncated })
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let mut body = payload::build(&request)?;
        self.options.request.merge_body(&mut body);
        let model = request.model.trim_start_matches("models/");
        let url = format!(
            "{}/v1beta/models/{}:streamGenerateContent?alt=sse",
            self.options.base_url.trim_end_matches('/'),
            utf8_percent_encode(model, PATH_SEGMENT),
        );
        let key = &self.options.api_key;
        let response = http::post(
            &self.client,
            &url,
            key,
            !self.options.request.sets_key_header(),
            &body,
            &cancel,
        )
        .await
        .map_err(|err| self.options.request.scrub_error(err))?;
        let translator = stream::Translator::new(request.model, key.clone());
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            ErrorScrub::new(Some(key.clone()), &self.options.request),
        ))
    }
}

#[cfg(test)]
mod tests;
