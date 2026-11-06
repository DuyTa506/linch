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
//! - A forced `tool_choice` (`required` or a named tool) with thinking on
//!   is a 400, so it fails locally instead.

use std::time::Duration;

use async_trait::async_trait;
use linch_core::provider::Thinking;
use linch_core::{
    JsonValue, Provider, ProviderError, ProviderRequest, ProviderStream, ReasoningEffort,
    ToolChoice, Usage,
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
}

impl DeepSeekOptions {
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
        let response = http::post(&self.client, &url, Some(key), &body, &cancel).await?;
        let translator =
            Translator::new(request.model, Some(ReasoningField::ReasoningContent), usage);
        Ok(stream::events(
            response.bytes_stream(),
            translator,
            cancel,
            Some(key.clone()),
        ))
    }
}

/// The request body for `request`.
pub(crate) fn build_payload(request: &ProviderRequest) -> Result<JsonValue, ProviderError> {
    let thinking = !matches!(request.thinking, Some(Thinking::Disabled {}));
    if thinking
        && matches!(
            request.tool_choice,
            Some(ToolChoice::Required {} | ToolChoice::Tool { .. })
        )
    {
        return Err(invalid(
            "DeepSeek rejects a forced tool_choice while thinking is on; \
             use tool_choice auto, or disable thinking",
        ));
    }
    if request.output_schema.is_some() {
        return Err(invalid(
            "the DeepSeek adapter does not support output_schema yet",
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
