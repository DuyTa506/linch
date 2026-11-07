//! The failure policy of one step: a timeout per attempt and retries with
//! exponential backoff.

use std::future::Future;
use std::time::Duration;

use tokio_util::sync::CancellationToken;

use super::error::WorkflowError;

/// Retries with exponential backoff for a failed step.
///
/// Only the winning attempt is journaled, so a retried step still replays
/// once; the function must be safe to run more than once. A step error and
/// a timeout are retried; an encode error is not, since it fails the same
/// way every time, and neither is a cancelled workflow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retry {
    /// Most attempts, the first one included; 0 counts as 1.
    pub max_attempts: u32,
    /// Wait before the first retry; each later retry waits twice as long.
    pub base_delay: Duration,
    /// Longest wait between two attempts.
    pub max_delay: Duration,
}

impl Retry {
    /// Up to `max_attempts` attempts, waiting 1 s before the first retry,
    /// doubling up to 30 s.
    pub fn attempts(max_attempts: u32) -> Self {
        Self {
            max_attempts,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(30),
        }
    }

    /// The wait after failed attempt number `failed` (0 for the first).
    fn delay(&self, failed: u32) -> Duration {
        let factor = 2u32.saturating_pow(failed);
        self.base_delay.saturating_mul(factor).min(self.max_delay)
    }
}

/// The timeout one attempt gets: the call's own value, else the
/// workflow's default; a zero value means none.
pub(super) fn attempt_timeout(
    call: Option<Duration>,
    default: Option<Duration>,
) -> Option<Duration> {
    call.or(default).filter(|timeout| !timeout.is_zero())
}

/// Run `attempt` until it succeeds, fails with an error that is not
/// retried, or runs out of attempts.
///
/// Args:
///     label: The call, for timeout and abort errors.
///     timeout: Limit on each attempt; a retry gets the full time again.
///     retry: The retry policy; `None` is one attempt.
///     cancel: Checked before every retry.
///     attempt: Starts one attempt.
pub(super) async fn run_attempts<T, F, Fut>(
    label: &str,
    timeout: Option<Duration>,
    retry: Option<&Retry>,
    cancel: &CancellationToken,
    mut attempt: F,
) -> Result<T, WorkflowError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, WorkflowError>>,
{
    let max_attempts = retry.map_or(1, |retry| retry.max_attempts.max(1));
    let mut failed = 0;
    loop {
        let outcome = match timeout {
            Some(after) => tokio::time::timeout(after, attempt())
                .await
                .unwrap_or_else(|_| {
                    Err(WorkflowError::Timeout {
                        label: label.to_owned(),
                        after,
                    })
                }),
            None => attempt().await,
        };
        let retried = matches!(
            outcome,
            Err(WorkflowError::Step { .. } | WorkflowError::Timeout { .. })
        );
        let Some(retry) = retry.filter(|_| retried && failed + 1 < max_attempts) else {
            return outcome;
        };
        tokio::time::sleep(retry.delay(failed)).await;
        failed += 1;
        if cancel.is_cancelled() {
            return Err(WorkflowError::Aborted {
                label: label.to_owned(),
            });
        }
    }
}
