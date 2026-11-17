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
    /// The workflow's budget is spent, so no further agent call starts. The
    /// calls that finished stay journaled.
    #[error("workflow budget exhausted ({spent_tokens} tokens, ${spent_usd:.4} spent)")]
    BudgetExceeded {
        /// Tokens the workflow's agent calls have spent, replayed ones
        /// included.
        spent_tokens: u64,
        /// The priced cost of those calls in USD.
        spent_usd: f64,
        /// The token cap, if the budget has one.
        max_tokens: Option<u64>,
        /// The USD cap, if the budget has one.
        max_cost_usd: Option<f64>,
    },
    /// A cap on what one workflow may do is reached (see
    /// [`WorkflowOptions::max_records`](super::WorkflowOptions::max_records)).
    #[error("workflow limit reached: at most {max} {what}")]
    LimitExceeded {
        /// What is capped, such as `journal records`.
        what: &'static str,
        /// The cap.
        max: u64,
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
