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

use super::{ProviderEvent, ProviderRequest, StreamError};

/// The event stream of one provider call.
pub type ProviderStream = BoxStream<'static, Result<ProviderEvent, ProviderError>>;

/// A model provider.
#[async_trait]
pub trait Provider: Send + Sync {
    /// Stable identifier, for example `deepseek` or `scripted`.
    fn id(&self) -> &str;

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
    /// The request never got a response (connect, TLS, timeout, reset).
    #[error("provider transport error: {message}")]
    Transport {
        /// Redacted description.
        message: String,
    },
    /// The provider answered with a non-success HTTP status.
    #[error("provider returned HTTP {status}: {message}")]
    Status {
        /// HTTP status code.
        status: u16,
        /// Server-requested delay before retrying, from `Retry-After`.
        retry_after: Option<Duration>,
        /// Redacted summary of the error body.
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
    /// Whether retrying the same request may succeed.
    pub fn retryable(&self) -> bool {
        match self {
            Self::Transport { .. } => true,
            Self::Status { status, .. } => *status == 429 || *status >= 500,
            Self::InvalidRequest { .. } | Self::Stream(_) | Self::Cancelled => false,
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
        assert!(!status(400).retryable());
        assert!(!status(401).retryable());
        assert!(
            ProviderError::Transport {
                message: "reset".into()
            }
            .retryable()
        );
        assert!(!ProviderError::Cancelled.retryable());
    }
}
