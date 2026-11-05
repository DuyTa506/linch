//! Errors shared by the kernel.

use std::fmt;
use std::sync::Arc;

/// Any error a teardown, listener, or plugin setup can report.
pub type BoxError = Arc<dyn std::error::Error + Send + Sync + 'static>;

/// Errors raised by the kernel.
///
/// The type is cheap to clone so one failed teardown can be reported to every
/// caller waiting on it.
#[derive(Debug, Clone, thiserror::Error)]
pub enum KernelError {
    /// Invalid kernel or plugin configuration (Python `ConfigError`).
    #[error("{0}")]
    Config(String),
    /// The scope is disposing or disposed and accepts no new registrations.
    #[error("effect scope is disposing or disposed")]
    Closed,
    /// No live registration resolves the requested service key.
    #[error("no service {key:?} registered on context {label:?}")]
    MissingService {
        /// The requested service key.
        key: String,
        /// Label of the context the lookup started from.
        label: String,
    },
    /// A service resolved, but its value is not of the requested type.
    #[error("service {key:?} is not a {expected}")]
    ServiceType {
        /// The requested service key.
        key: String,
        /// Name of the requested Rust type.
        expected: &'static str,
    },
    /// A failure raised by user code (teardown, listener, or plugin setup).
    #[error("{0}")]
    Failed(BoxError),
    /// Several teardown failures, collected after every cleanup was attempted.
    #[error("{} cleanup operations failed", .0.len())]
    Cleanup(Vec<KernelError>),
}

impl KernelError {
    /// Wrap any error raised by user code.
    pub fn failed(error: impl std::error::Error + Send + Sync + 'static) -> Self {
        Self::Failed(Arc::new(error))
    }

    /// Build a [`KernelError::Failed`] from a plain message.
    ///
    /// ```
    /// let err = linch_kernel::KernelError::msg("cleanup failed");
    /// assert_eq!(err.to_string(), "cleanup failed");
    /// ```
    pub fn msg(message: impl Into<String>) -> Self {
        Self::failed(Message(message.into()))
    }

    pub(crate) fn config(message: impl Into<String>) -> Self {
        Self::Config(message.into())
    }

    /// Collapse collected teardown errors: none is `Ok`, one is itself, more are `Cleanup`.
    pub(crate) fn collect(mut errors: Vec<KernelError>) -> Result<(), KernelError> {
        match errors.len() {
            0 => Ok(()),
            1 => Err(errors.remove(0)),
            _ => Err(Self::Cleanup(errors)),
        }
    }
}

#[derive(Debug)]
struct Message(String);

impl fmt::Display for Message {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Message {}
