//! Request settings of a [`ProviderConfig`](super::ProviderConfig): the
//! data form of Python's `extra_body`, `default_headers`, `timeout` and
//! the chat options.

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use linch_core::{JsonObject, ProviderError};
use linch_providers::{ChatOptions, RequestOptions};
use serde::{Deserialize, Serialize};

use super::redact;

/// Extra settings for the HTTP requests of any provider.
///
/// `Serialize` hides the header values, which can be credentials;
/// [`AgentConfig::to_value_with_secrets`](crate::AgentConfig::to_value_with_secrets)
/// keeps them.
#[derive(Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestConfig {
    /// Top-level fields merged into every request body, for vendor
    /// parameters (`top_k`, `cache_salt`, ...). They replace a field the
    /// adapter sets, except `model`, `stream` and `temperature`.
    #[serde(default, skip_serializing_if = "JsonObject::is_empty")]
    pub extra_body: JsonObject,
    /// Headers sent with every request, for gateways. The key headers
    /// (`authorization`, `x-api-key`, `x-goog-api-key`) are refused, since
    /// the key comes from the environment, and so are `host`,
    /// `content-length`, `transfer-encoding`, `connection`, `upgrade`, `te`
    /// and `trailer`.
    #[serde(
        default,
        skip_serializing_if = "BTreeMap::is_empty",
        serialize_with = "redact::values"
    )]
    pub default_headers: BTreeMap<String, String>,
    /// Longest silence, in milliseconds, between two chunks of a response
    /// before the call fails; the provider's own limit (5 to 10 minutes)
    /// when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout_ms: Option<std::num::NonZeroU64>,
}

impl RequestConfig {
    pub(super) fn is_default(&self) -> bool {
        self == &Self::default()
    }

    /// The options an adapter takes.
    ///
    /// # Errors
    ///
    /// [`ProviderError::InvalidRequest`] when a header is not allowed.
    pub fn options(&self) -> Result<RequestOptions, ProviderError> {
        let mut options = RequestOptions::default();
        options.extra_body = self.extra_body.clone();
        options.timeout = self.timeout_ms.map(|ms| Duration::from_millis(ms.get()));
        for (name, value) in &self.default_headers {
            options = options.header(name, value)?;
        }
        Ok(options)
    }
}

impl fmt::Debug for RequestConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RequestConfig")
            .field("extra_body", &self.extra_body)
            .field(
                "default_headers",
                &self.default_headers.keys().collect::<Vec<_>>(),
            )
            .field("timeout_ms", &self.timeout_ms)
            .finish()
    }
}

/// Extra settings for the Chat Completions providers (`deepseek`,
/// `openai-chat`, `vllm`, `sglang`, `llamacpp`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChatConfig {
    /// Sent as `parallel_tool_calls` when the request has tools; the
    /// server's default when absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parallel_tool_calls: Option<bool>,
    /// Whether to ask for usage in the last chunk (`stream_options`); on
    /// when absent.
    #[serde(default = "yes", skip_serializing_if = "is_true")]
    pub include_stream_options: bool,
    /// Fields of `chat_template_kwargs`, for servers that apply a chat
    /// template. With thinking off, the server's thinking switch is merged
    /// over these.
    #[serde(default, skip_serializing_if = "JsonObject::is_empty")]
    pub chat_template_kwargs: JsonObject,
    /// Native sampling fields (`top_k`, `min_p`, ...) merged into the
    /// body, under `extra_body`.
    #[serde(default, skip_serializing_if = "JsonObject::is_empty")]
    pub sampling_params: JsonObject,
}

fn yes() -> bool {
    true
}

fn is_true(value: &bool) -> bool {
    *value
}

impl Default for ChatConfig {
    fn default() -> Self {
        Self {
            parallel_tool_calls: None,
            include_stream_options: true,
            chat_template_kwargs: JsonObject::new(),
            sampling_params: JsonObject::new(),
        }
    }
}

impl ChatConfig {
    pub(super) fn is_default(&self) -> bool {
        self == &Self::default()
    }

    /// The options an adapter takes.
    pub fn options(&self) -> ChatOptions {
        ChatOptions {
            parallel_tool_calls: self.parallel_tool_calls,
            include_stream_options: self.include_stream_options,
            chat_template_kwargs: self.chat_template_kwargs.clone(),
            sampling_params: self.sampling_params.clone(),
        }
    }
}
