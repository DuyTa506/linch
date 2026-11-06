//! The worker crate's error type.

use linch_core::RunId;
use thiserror::Error;

use crate::job::JobId;

/// A queue, schedule or worker call failed.
#[derive(Debug, Error)]
pub enum WorkerError {
    /// The database call failed.
    #[error("worker database call failed")]
    Database(#[from] sqlx::Error),
    /// A cron pattern did not parse, or has no next fire.
    #[error("invalid cron pattern {pattern:?}")]
    Cron {
        /// The pattern as given.
        pattern: String,
        /// Why it is unusable.
        #[source]
        source: croner::errors::CronError,
    },
    /// An argument or a configuration value is unusable.
    #[error("invalid worker input: {0}")]
    Invalid(String),
    /// The run already has an active job; a run has at most one, so that
    /// two jobs never claim it from each other.
    #[error("run {run_id} already has active job {}", job.0)]
    RunBusy {
        /// The run.
        run_id: RunId,
        /// Its active job.
        job: JobId,
    },
}

/// `error` and every source under it, joined with `: `, for job records and
/// logs (the top-level messages of store errors name no cause).
pub(crate) fn chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(": ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}

/// Refuse a blank name.
pub(crate) fn non_blank(value: &str, what: &str) -> Result<(), WorkerError> {
    if value.trim().is_empty() {
        return Err(WorkerError::Invalid(format!("{what} must not be blank")));
    }
    Ok(())
}
