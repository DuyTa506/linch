//! [`WorkflowError`]: every way a workflow call or run can end early.

use thiserror::Error;

use super::journal::JournalStoreError;

/// The error a step function returns, boxed.
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// A workflow call or run ended without a value.
#[derive(Debug, Error)]
pub enum WorkflowError {
    /// A step's function returned an error.
    #[error("step {name} failed: {source}")]
    Step {
        /// The step's name.
        name: String,
        /// The function's error.
        #[source]
        source: BoxError,
    },
    /// A value could not be journaled as JSON, so it could never replay.
    #[error("{label} cannot be journaled: {message}")]
    Encode {
        /// The call, for example `step fetch`.
        label: String,
        /// Why encoding failed.
        message: String,
    },
    /// The journal store failed, including a lease another claim fenced
    /// out ([`JournalStoreError::LeaseLost`]).
    #[error(transparent)]
    Store(#[from] JournalStoreError),
}
