//! [`WorkflowError`]: every way a workflow call or run can end early.

use std::time::Duration;

use thiserror::Error;

use super::journal::{Interrupt, JournalStoreError};

/// The error a step function returns, boxed.
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// A workflow call or run ended without a value.
#[derive(Debug, Error)]
pub enum WorkflowError {
    /// An interrupt has no answer yet: the workflow is parked at a
    /// decision point, not failed. Propagate it with `?`; even when the
    /// workflow function drops it, [`run_workflow`](super::run_workflow)
    /// still reports the suspend.
    #[error("workflow suspended at interrupt {}", .0.key)]
    Suspended(Interrupt),
    /// A step's function returned an error.
    #[error("step {name} failed: {source}")]
    Step {
        /// The step's name.
        name: String,
        /// The function's error.
        #[source]
        source: BoxError,
    },
    /// A step attempt, or the whole workflow, ran past its time.
    #[error("{label} timed out after {after:?}")]
    Timeout {
        /// The call, for example `step fetch`, or `workflow`.
        label: String,
        /// The limit it ran past.
        after: Duration,
    },
    /// The workflow's cancel token fired before the call ran.
    #[error("{label} was aborted")]
    Aborted {
        /// The call that did not run.
        label: String,
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
