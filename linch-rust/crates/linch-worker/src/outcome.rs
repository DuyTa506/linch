//! How an attempt at a job ended, and how a run error maps onto that.

use linch_core::{JournalError, RunError, RunId, StoreError};

use crate::error::chain;

/// How an attempt at a job ended.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    /// The run is terminal or waits for approval: delete the job.
    Done,
    /// Try again after the retry delay, counting a failure.
    Retry(String),
    /// A start job whose session has a live run: try again later, without
    /// counting a failure.
    Busy(String),
    /// The job can never succeed: keep it, failed, with this error.
    Fail(String),
    /// The run can never continue either: mark it failed, then the job.
    FailRun {
        /// The run to fail.
        run_id: RunId,
        /// Why; the run's `Error` event and the job's error.
        error: String,
    },
    /// Another owner claimed the run since this attempt did: it drives the
    /// run now, so delete the job without a retry, which would take the run
    /// back from it.
    Fenced(String),
    /// Another worker holds the job now: leave it alone.
    Lost,
}

/// The store error inside `error`, whichever layer reported it.
fn store_error(error: &RunError) -> Option<&StoreError> {
    match error {
        RunError::Store(error) | RunError::Journal(JournalError::Store(error)) => Some(error),
        _ => None,
    }
}

/// Map an error of `run_id` onto the job's outcome.
pub(crate) fn classify(error: &RunError, run_id: &RunId) -> Outcome {
    match store_error(error) {
        Some(StoreError::LeaseLost { .. }) => return Outcome::Fenced(chain(error)),
        // Nothing was written; the job waits for that run.
        Some(StoreError::SessionBusy { .. }) => return Outcome::Busy(chain(error)),
        _ => {}
    }
    match error {
        RunError::Terminal { .. } => Outcome::Done,
        // Its durable state does not fit, or no agent has its contract.
        RunError::ContractMismatch { .. } | RunError::Reconcile(_) => Outcome::FailRun {
            run_id: run_id.clone(),
            error: chain(error),
        },
        // An approve that came late: the run itself is fine.
        RunError::NotSuspended { .. }
        // A user_prompt_submit hook refused the prompt; a retry sends the same prompt.
        | RunError::PromptBlocked { .. } => Outcome::Fail(chain(error)),
        // Includes a session append conflict.
        RunError::Store(_) | RunError::Journal(_) => Outcome::Retry(chain(error)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use linch_core::{ReconcileError, RunStatus, SessionId};

    fn run() -> RunId {
        RunId::new("r1")
    }

    fn classify(error: &RunError) -> Outcome {
        super::classify(error, &run())
    }

    #[test]
    fn run_errors_map_onto_outcomes() {
        let terminal = RunError::Terminal {
            run_id: run(),
            status: RunStatus::Completed,
        };
        assert_eq!(classify(&terminal), Outcome::Done);
        let digest = "0".repeat(64).parse().unwrap();
        let mismatch = RunError::ContractMismatch {
            run_id: run(),
            expected: digest,
            found: digest,
        };
        let fails_run =
            |outcome| matches!(outcome, Outcome::FailRun { run_id, .. } if run_id == run());
        assert!(fails_run(classify(&mismatch)));
        let not_suspended = RunError::NotSuspended { run_id: run() };
        assert!(matches!(classify(&not_suspended), Outcome::Fail(_)));
        let conflict = RunError::Store(StoreError::Conflict {
            message: "session".into(),
        });
        assert!(matches!(classify(&conflict), Outcome::Retry(_)));
        let busy = RunError::Store(StoreError::SessionBusy {
            session_id: SessionId::new("s1"),
            run_id: RunId::new("r0"),
        });
        assert!(matches!(classify(&busy), Outcome::Busy(text) if text.contains("busy")));
        let reconcile = RunError::Reconcile(ReconcileError::MissingToolCalls);
        assert!(fails_run(classify(&reconcile)));
    }

    #[test]
    fn a_lost_run_lease_drops_the_job() {
        let lost = || StoreError::LeaseLost {
            run_id: run(),
            epoch: 1,
        };
        // A claim or a read reports it as a store error, a journal write
        // through the journal.
        for error in [
            RunError::Store(lost()),
            RunError::Journal(JournalError::Store(lost())),
        ] {
            assert!(matches!(classify(&error), Outcome::Fenced(text) if text.contains("lease")));
        }
    }
}
