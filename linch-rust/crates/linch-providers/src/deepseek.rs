//! DeepSeek's native Chat Completions endpoint.
//!
//! Wire rules, checked against `api-docs.deepseek.com` on 2026-11-06:
//! - `thinking: {"type": "enabled" | "disabled"}` is always sent, because
//!   DeepSeek thinks by default; it takes precedence over `reasoning_effort`.
//! - `reasoning_effort` takes `low`, `high` or `max`, sent only with
//!   thinking on.
//! - With tools in the request, every earlier assistant turn's reasoning
//!   must be sent back as `reasoning_content`, or the call fails with 400.
//!   With thinking on, a turn that has no thinking (made with thinking
//!   off, or by another provider) sends `""`: checked live on 2026-11-06,
//!   leaving the field out is that 400 and `""` is accepted.
//! - `tool_choice` takes `none`, `auto`, `required` or a named function; a
//!   forced one (`required` or named) with thinking on is a 400 (checked
//!   2026-11-17 against <https://api-docs.deepseek.com/api/create-chat-completion>,
//!   which says thinking mode supports neither `required` nor a named
//!   choice), so [`Provider::tool_choice_refusal`] refuses it when the agent
//!   opens and the request fails locally too. Thinking is on unless
//!   [`Thinking::Disabled`].
//! - `output_schema` is refused in a request: `response_format` takes
//!   only `text` or `json_object`, which does not carry the schema
//!   (<https://api-docs.deepseek.com/guides/json_mode>).
//!   [`Provider::native_output_schema`] is false, so a run with a schema
//!   offers its final tool instead, forced only with thinking off.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::provider::Thinking;
use linch_core::{
    ContextSource, JsonValue, ModelInfo, ModelsError, PromptCache, Provider, ProviderCapabilities,
    ProviderError, ProviderRequest, ProviderStream, ReasoningEffort, Support, ThinkingCaps,
    ToolChoice, ToolChoiceCaps, Usage,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

use crate::ApiKey;
use crate::openai_compat::stream::{ReasoningField, Translator, WireUsage};
use crate::openai_compat::{http, payload, stream};

/// DeepSeek's API root.
pub const DEFAULT_BASE_URL: &str = "https://api.deepseek.com";

/// The environment variable [`DeepSeekOptions::from_env`] reads.
pub const API_KEY_ENV: &str = "DEEPSEEK_API_KEY";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// DeepSeek sends keep-alive comments while it queues a request, so a long
/// silence means a dead connection.
const READ_TIMEOUT: Duration = Duration::from_secs(300);

/// Credentials and endpoint for [`DeepSeek`].
///
/// The model is not an option: it comes from each [`ProviderRequest`].
#[derive(Debug, Clone)]
pub struct DeepSeekOptions {
    /// The API key, sent as a bearer token.
    pub api_key: ApiKey,
    /// API root without the `/chat/completions` path.
    pub base_url: String,
    /// The context window of every model; `None` gives 128000, Python's
    /// conservative fallback.
    pub context_window: Option<u64>,
}

impl DeepSeekOptions {
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

/// The DeepSeek provider.
#[derive(Debug, Clone)]
pub struct DeepSeek {
    client: reqwest::Client,
    options: DeepSeekOptions,
}

impl DeepSeek {
    /// Build a provider with its own HTTP client.
    ///
    /// # Errors
    ///
    /// [`ProviderError::Transport`] when the HTTP client cannot be built,
    /// for example when no TLS roots are available.
    pub fn new(options: DeepSeekOptions) -> Result<Self, ProviderError> {
        let client = crate::transport::client(CONNECT_TIMEOUT, READ_TIMEOUT)
            .map_err(|err| http::transport(Some(&options.api_key), &err))?;
        Ok(Self { client, options })
    }
}

#[async_trait]
impl Provider for DeepSeek {
    fn id(&self) -> &str {
        "deepseek"
    }

    fn context_window(&self, _model: &str) -> u64 {
        (self.options.context_window).unwrap_or(linch_core::DEFAULT_CONTEXT_WINDOW)
    }

    /// DeepSeek has `json_object` mode only, which does not carry the
    /// schema: a run with one offers its final tool.
    fn native_output_schema(&self) -> bool {
        false
    }

    fn tool_choice_refusal(
        &self,
        choice: &ToolChoice,
        thinking: Option<&Thinking>,
    ) -> Option<String> {
        tool_choice_refusal(choice, thinking).map(str::to_owned)
    }

    /// DeepSeek's own rules: its `thinking` switch (no budget), effort as
    /// `low`, `high` or `max`, no schema, and images only for the models
    /// that list `image` among their input modalities.
    fn capabilities(&self, model: &str) -> ProviderCapabilities {
        let configured = self.options.context_window;
        ProviderCapabilities {
            provider: self.id().to_owned(),
            model: model.to_owned(),
            declared: true,
            context_window: self.context_window(model),
            context_source: if configured.is_some() {
                ContextSource::Configured
            } else {
                ContextSource::Default
            },
            native_output_schema: false,
            tool_choice: ToolChoiceCaps::probe(|choice, thinking| {
                self.tool_choice_refusal(choice, thinking)
            }),
            thinking: ThinkingCaps {
                disable: true,
                budget: false,
                effort: true,
                reasoning_text: true,
            },
            vision: Support::ModelDependent,
            streams_tool_arguments: true,
            prompt_cache: PromptCache::Automatic,
            stop_sequences: true,
        }
    }

    /// `GET /models`, which also gives each model's window, output limit
    /// and input modalities.
    async fn list_models(&self) -> Result<Vec<ModelInfo>, ModelsError> {
        let url = format!("{}/models", self.options.base_url.trim_end_matches('/'));
        let key = &self.options.api_key;
        let bearer = format!("Bearer {}", key.expose());
        let headers = [("authorization", bearer.as_str())];
        let value = crate::models::get_json(&self.client, &url, &headers, Some(key)).await?;
        let row = crate::models::OpenAiRow {
            context: Some("context_window"),
            output: Some("max_output_tokens"),
            modalities: true,
        };
        Ok(crate::models::openai_list(&value, row))
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let body = build_payload(&request)?;
        let url = format!(
            "{}/chat/completions",
            self.options.base_url.trim_end_matches('/')
        );
        let key = &self.options.api_key;
        let overflow = &crate::overflow::DEEPSEEK;
        let response = http::post(&self.client, &url, Some(key), &body, overflow, &cancel).await?;
        let translator =
            Translator::new(request.model, Some(ReasoningField::ReasoningContent), usage)
                .refusing(overflow);
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            Some(key.clone()),
        ))
    }
}

/// Why DeepSeek refuses `choice` with `thinking`: a forced choice
/// (`required` or a named tool) while thinking is on, which is any state
/// but [`Thinking::Disabled`]; `None` when it takes it.
pub(crate) fn tool_choice_refusal(
    choice: &ToolChoice,
    thinking: Option<&Thinking>,
) -> Option<&'static str> {
    let forced = matches!(choice, ToolChoice::Required {} | ToolChoice::Tool { .. });
    let thinks = !matches!(thinking, Some(Thinking::Disabled {}));
    (forced && thinks).then_some(
        "DeepSeek rejects a forced tool_choice while thinking is on; use tool_choice auto, \
         or disable thinking",
    )
}

/// The request body for `request`.
pub(crate) fn build_payload(request: &ProviderRequest) -> Result<JsonValue, ProviderError> {
    let thinking = !matches!(request.thinking, Some(Thinking::Disabled {}));
    if let Some(refusal) = (request.tool_choice.as_ref())
        .and_then(|choice| tool_choice_refusal(choice, request.thinking.as_ref()))
    {
        return Err(invalid(refusal));
    }
    if request.output_schema.is_some() {
        return Err(invalid(
            "the DeepSeek adapter does not support output_schema; a run offers its final tool",
        ));
    }
    let mut messages = payload::messages(&request.system, &request.messages, true)?;
    if thinking {
        for message in &mut messages {
            if message["role"] == "assistant" && message.get("reasoning_content").is_none() {
                message["reasoning_content"] = "".into();
            }
        }
    }
    let mut body = json!({
        "model": request.model,
        "messages": messages,
        "stream": true,
        "stream_options": {"include_usage": true},
        "thinking": {"type": if thinking { "enabled" } else { "disabled" }},
    });
    if !request.tools.is_empty() {
        body["tools"] = payload::tools(&request.tools).into();
    }
    if let Some(choice) = &request.tool_choice {
        body["tool_choice"] = payload::tool_choice(choice);
    }
    if let Some(max_tokens) = request.max_output_tokens {
        body["max_tokens"] = max_tokens.into();
    }
    if let Some(temperature) = request.temperature {
        body["temperature"] = temperature.into();
    }
    if !request.stop_sequences.is_empty() {
        body["stop"] = request.stop_sequences.clone().into();
    }
    if let (true, Some(effort)) = (thinking, request.effort) {
        body["reasoning_effort"] = effort_value(effort).into();
    }
    Ok(body)
}

/// DeepSeek's own effort levels, following its documented aliases:
/// `medium` and `xhigh` run as `high`.
fn effort_value(effort: ReasoningEffort) -> &'static str {
    match effort {
        ReasoningEffort::Low => "low",
        ReasoningEffort::Medium | ReasoningEffort::High | ReasoningEffort::Xhigh => "high",
        ReasoningEffort::Max => "max",
    }
}

/// `prompt_tokens` includes cache hits; Linch counts them apart.
pub(crate) fn usage(wire: &WireUsage) -> Usage {
    let cache_read = wire
        .prompt_cache_hit_tokens
        .or_else(|| wire.prompt_tokens_details.as_ref()?.cached_tokens)
        .unwrap_or(0);
    Usage {
        input_tokens: wire.prompt_tokens.saturating_sub(cache_read),
        output_tokens: wire.completion_tokens,
        cache_read_tokens: cache_read,
        cache_creation_tokens: 0,
    }
}

fn invalid(message: &str) -> ProviderError {
    ProviderError::InvalidRequest {
        message: message.into(),
    }
}

#[cfg(test)]
mod tests;
