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
//! - Structured output (checked 2026-11-17) is the native
//!   `response_format` `{"type": "json_schema", "json_schema": {name,
//!   strict, schema, description?}}`. It shapes only the text answer, so
//!   the model still calls tools freely. `strict: true` asks the API to
//!   enforce the schema, which then needs `additionalProperties: false`
//!   and every property required. The server profiles send no schema
//!   (their guided decoding would bind tool-call text to it as well, which
//!   was not checked against a live server) and refuse one in a request:
//!   [`Provider::native_output_schema`] is false for them, so a run with a
//!   schema offers its final tool there instead.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    ContextSource, DEFAULT_CONTEXT_WINDOW, JsonObject, ModelInfo, ModelsError, Provider,
    ProviderCapabilities, ProviderError, ProviderRequest, ProviderStream, Thinking, ThinkingCaps,
    ToolChoice, ToolChoiceCaps,
};
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::context::{self, ContextRule, Probed};
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
pub(crate) use profile::{ChatProfile, ModelList, ToolChoiceSupport, effort_value};

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
    /// The context window of every model; `None` takes Python's table
    /// (gpt-5.5 and gpt-5.4 400000, o1 200000, others 128000).
    pub context_window: Option<u64>,
}

impl OpenAiChatOptions {
    /// Options for the public endpoint with `api_key`.
    pub fn new(api_key: ApiKey) -> Self {
        Self {
            api_key,
            base_url: DEFAULT_BASE_URL.into(),
            context_window: None,
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
    /// The configured context window, which wins over the profile's rule.
    context_window: Option<u64>,
    /// What [`Provider::prepare`] read from the server.
    probed: Arc<OnceLock<Probed>>,
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
            options.context_window,
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
            options.context_window,
        )
    }

    fn build(
        profile: &'static ChatProfile,
        base_url: String,
        api_key: Option<ApiKey>,
        thinking_off: JsonObject,
        context_window: Option<u64>,
    ) -> Result<Self, ProviderError> {
        let client = crate::transport::client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(api_key.as_ref(), &err))?;
        Ok(Self {
            client,
            profile,
            base_url,
            api_key,
            thinking_off,
            context_window,
            probed: Arc::default(),
        })
    }
}

impl OpenAiChat {
    /// The window of `model` and where it comes from: the configured one,
    /// else the profile's table or what the server told
    /// [`Provider::prepare`]; a model neither names gets the default.
    fn window(&self, model: &str) -> (u64, ContextSource) {
        if let Some(window) = self.context_window {
            return (window, ContextSource::Configured);
        }
        let (found, source) = match self.profile.context {
            ContextRule::Known(table) => (context::in_table(table, model), ContextSource::Table),
            ContextRule::Models | ContextRule::Props => (
                context::probed_lookup(self.probed.get(), model),
                ContextSource::Server,
            ),
        };
        match found {
            Some(window) => (window, source),
            None => (DEFAULT_CONTEXT_WINDOW, ContextSource::Default),
        }
    }

    /// The request body `request` would be sent as; a test hook.
    #[cfg(test)]
    pub(crate) fn body(
        &self,
        request: &ProviderRequest,
    ) -> Result<linch_core::JsonValue, ProviderError> {
        profile::build_payload(self.profile, &self.thinking_off, request)
    }
}

#[async_trait]
impl Provider for OpenAiChat {
    fn id(&self) -> &str {
        self.profile.id
    }

    /// The configured window, else the profile's: OpenAI's table, or
    /// what the server told [`Provider::prepare`].
    fn context_window(&self, model: &str) -> u64 {
        self.window(model).0
    }

    /// Whether the profile sends a `json_schema` `response_format`; a
    /// server profile does not, so a run there offers its final tool.
    fn native_output_schema(&self) -> bool {
        self.profile.json_schema
    }

    /// The profile's `tool_choice` forms; thinking does not change them.
    fn tool_choice_refusal(
        &self,
        choice: &ToolChoice,
        _thinking: Option<&Thinking>,
    ) -> Option<String> {
        profile::tool_choice_refusal(self.profile, choice)
    }

    /// What this target's profile states, field by field; a server profile
    /// inherits nothing from OpenAI's.
    fn capabilities(&self, model: &str) -> ProviderCapabilities {
        let profile = self.profile;
        let (context_window, context_source) = self.window(model);
        ProviderCapabilities {
            provider: profile.id.to_owned(),
            model: model.to_owned(),
            declared: true,
            context_window,
            context_source,
            native_output_schema: profile.json_schema,
            tool_choice: ToolChoiceCaps::probe(|choice, thinking| {
                self.tool_choice_refusal(choice, thinking)
            }),
            thinking: ThinkingCaps {
                // The switch sends nothing when its kwargs are empty.
                disable: profile.template_thinking_switch && !self.thinking_off.is_empty(),
                budget: false,
                effort: profile.reasoning_effort,
                reasoning_text: profile.reasoning_field.is_some(),
            },
            vision: profile.vision,
            streams_tool_arguments: profile.streams_tool_arguments,
            prompt_cache: profile.prompt_cache,
            stop_sequences: profile.stop_sequences,
        }
    }

    /// `GET {base}/models`; a llama.cpp list has no windows, so its one
    /// model's comes from `/props`.
    async fn list_models(&self) -> Result<Vec<ModelInfo>, ModelsError> {
        let url = format!("{}/models", self.base_url.trim_end_matches('/'));
        let key = self.api_key.as_ref();
        let bearer = key.map(|key| format!("Bearer {}", key.expose()));
        let headers: Vec<(&str, &str)> = (bearer.as_deref())
            .map(|value| ("authorization", value))
            .into_iter()
            .collect();
        let value = crate::models::get_json(&self.client, &url, &headers, key).await?;
        let mut models = crate::models::openai_list(&value, self.profile.models.row);
        if self.profile.models.props_context {
            let window = context::props_window(&self.client, &self.base_url, key).await;
            for model in &mut models {
                model.context_window = model.context_window.or(window);
            }
        }
        Ok(models)
    }

    /// Ask a server for its context window once, unless one is configured.
    async fn prepare(&self) {
        if self.context_window.is_some() || self.probed.get().is_some() {
            return;
        }
        let key = self.api_key.as_ref();
        let probed = context::probe(&self.client, self.profile.context, &self.base_url, key).await;
        let _ = self.probed.set(probed);
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = profile::build_payload(self.profile, &self.thinking_off, &request)?;
        let url = format!("{}/chat/completions", self.base_url.trim_end_matches('/'));
        let key = self.api_key.as_ref();
        let overflow = self.profile.overflow;
        let response = http::post(&self.client, &url, key, &body, overflow, &cancel).await?;
        let translator = Translator::new(
            request.model,
            self.profile.reasoning_field,
            self.profile.usage,
        )
        .refusing(overflow);
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            self.api_key.clone(),
        ))
    }
}
