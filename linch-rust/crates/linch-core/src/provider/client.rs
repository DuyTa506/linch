//! The provider trait every model adapter implements.
//!
//! An adapter turns a [`ProviderRequest`] into a stream of normalized
//! [`ProviderEvent`]s. It never retries on its own; the caller decides, using
//! [`ProviderError::retryable`] and `retry_after`.

use std::time::Duration;

use async_trait::async_trait;
use futures::stream::BoxStream;
use thiserror::Error;
use tokio_util::sync::CancellationToken;

use super::{
    ModelInfo, ModelsError, ProviderCapabilities, ProviderEvent, ProviderRequest, StreamError,
    Thinking, ToolChoice, ToolChoiceCaps,
};

/// The event stream of one provider call.
pub type ProviderStream = BoxStream<'static, Result<ProviderEvent, ProviderError>>;

/// The context window a provider reports for a model it knows nothing
/// about (Python's `ProviderCapabilities.context_window` default).
pub const DEFAULT_CONTEXT_WINDOW: u64 = 128_000;

/// A model provider.
#[async_trait]
pub trait Provider: Send + Sync {
    /// Stable identifier, for example `deepseek` or `scripted`.
    fn id(&self) -> &str;

    /// The most tokens `model` takes in one request, prompt and output
    /// together. It never does I/O: a value a server reports is read once
    /// by [`Self::prepare`]. The run derives compaction's trigger and the
    /// skill listing's size from it.
    fn context_window(&self, _model: &str) -> u64 {
        DEFAULT_CONTEXT_WINDOW
    }

    /// One-time discovery before the provider is used, such as asking a
    /// self-hosted server for its context window. It never fails: a probe
    /// that does not answer leaves the configured or default value. The
    /// `linch` facade calls it when it opens an agent; a host that builds
    /// one in code calls it before [`crate::AgentBuilder::build`].
    async fn prepare(&self) {}

    /// Whether the provider takes a run's output schema in the request
    /// ([`ProviderRequest::output_schema`]), in its own structured-output
    /// form; true unless the adapter says otherwise. A provider without it
    /// is never sent the schema: the run offers a final tool instead, whose
    /// input schema is the output schema and whose call ends the run with
    /// that input as the answer (Python's `structured_output_terminal_tool`).
    fn native_output_schema(&self) -> bool {
        true
    }

    /// Why the provider cannot send `choice` in a request with `thinking`;
    /// `None` when it can (the default: every choice). It never does I/O.
    /// The agent refuses a `tool_choice` this names when it is built and
    /// when a run brings its own, rather than failing each call; an
    /// adapter checks the same rule again when it builds the request.
    fn tool_choice_refusal(
        &self,
        _choice: &ToolChoice,
        _thinking: Option<&Thinking>,
    ) -> Option<String> {
        None
    }

    /// What the adapter supports for `model`, as plain data. It never does
    /// I/O: a window the server reports is read by [`Self::prepare`].
    ///
    /// Every adapter overrides it and states each field itself, so none
    /// takes another's rules by default. The default answers only what this
    /// trait already says, with `declared` false.
    fn capabilities(&self, model: &str) -> ProviderCapabilities {
        ProviderCapabilities::undeclared(
            self.id(),
            model,
            self.context_window(model),
            self.native_output_schema(),
            ToolChoiceCaps::probe(|choice, thinking| self.tool_choice_refusal(choice, thinking)),
        )
    }

    /// The models the provider's server lists, when its API has such a
    /// list. It asks the server on every call.
    ///
    /// # Errors
    ///
    /// [`ModelsError::Unsupported`] by default; an adapter's own failures
    /// are [`ModelsError::Provider`], with the key and the URL left out.
    async fn list_models(&self) -> Result<Vec<ModelInfo>, ModelsError> {
        Err(ModelsError::Unsupported {
            provider: self.id().to_owned(),
        })
    }

    /// What the server lists about `model`: its entry of
    /// [`Self::list_models`].
    ///
    /// # Errors
    ///
    /// [`ModelsError::NotFound`] when the list lacks it, else as
    /// [`Self::list_models`].
    async fn model_info(&self, model: &str) -> Result<ModelInfo, ModelsError> {
        let listed = self.list_models().await?;
        (listed.into_iter().find(|info| info.id == model)).ok_or_else(|| ModelsError::NotFound {
            model: model.to_owned(),
        })
    }

    /// Start one call. The stream ends after `message_end` or an error.
    /// Cancelling `cancel` ends the stream with [`ProviderError::Cancelled`].
    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError>;
}

/// Why a provider call failed.
///
/// Messages are written by the adapter and must never contain credentials or
/// request headers, including ones a server echoes back in an error body.
#[derive(Debug, Error)]
pub enum ProviderError {
    /// The connection was never made (refused, DNS, TLS handshake, connect
    /// timeout), so the request never went out and cost nothing.
    #[error("provider connect error: {message}")]
    Connect {
        /// Redacted description.
        message: String,
    },
    /// The request went out but no response came back (reset, read
    /// timeout), or the stream broke after it began: the provider may have
    /// billed it.
    #[error("provider transport error: {message}")]
    Transport {
        /// Redacted description.
        message: String,
    },
    /// The provider answered with a non-success HTTP status. Retryable for
    /// 408, 429 and 5xx.
    #[error("provider returned HTTP {status}: {message}")]
    Status {
        /// HTTP status code.
        status: u16,
        /// Server-requested delay before retrying, from `Retry-After`.
        retry_after: Option<Duration>,
        /// Redacted summary of the error body.
        message: String,
    },
    /// The prompt does not fit the model's context window: the provider
    /// refused it as too long. Retrying the same request cannot succeed;
    /// a run with compaction shortens its view and calls again. Each
    /// adapter maps its own provider's error to it.
    #[error("prompt exceeds the model's context window: {message}")]
    ContextLength {
        /// HTTP status the provider answered with; `None` for an error
        /// that arrived inside a stream with no status of its own.
        status: Option<u16>,
        /// Redacted summary of the error.
        message: String,
    },
    /// The request cannot be sent as built (an adapter-side check failed).
    #[error("invalid provider request: {message}")]
    InvalidRequest {
        /// What is wrong with the request.
        message: String,
    },
    /// The provider's event stream broke the stream contract.
    #[error(transparent)]
    Stream(#[from] StreamError),
    /// The call was cancelled.
    #[error("provider call cancelled")]
    Cancelled,
}

impl ProviderError {
    /// Whether retrying the same request may succeed: no connection or no
    /// response at all, a request timeout (408), a rate limit (429) or a
    /// server error (5xx).
    pub fn retryable(&self) -> bool {
        match self {
            Self::Connect { .. } | Self::Transport { .. } => true,
            Self::Status { status, .. } => matches!(*status, 408 | 429) || *status >= 500,
            Self::ContextLength { .. }
            | Self::InvalidRequest { .. }
            | Self::Stream(_)
            | Self::Cancelled => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_transient_failures_are_retryable() {
        let status = |status| ProviderError::Status {
            status,
            retry_after: None,
            message: String::new(),
        };
        assert!(status(429).retryable());
        assert!(status(503).retryable());
        assert!(
            status(408).retryable(),
            "a request timeout, as Python retries it"
        );
        assert!(!status(400).retryable());
        assert!(!status(409).retryable());
        assert!(!status(401).retryable());
        assert!(
            ProviderError::Transport {
                message: "reset".into()
            }
            .retryable()
        );
        assert!(
            ProviderError::Connect {
                message: "refused".into()
            }
            .retryable()
        );
        assert!(!ProviderError::Cancelled.retryable());
        let overflow = ProviderError::ContextLength {
            status: Some(400),
            message: "prompt is too long".into(),
        };
        assert!(!overflow.retryable(), "the same prompt is too long again");
    }
}
