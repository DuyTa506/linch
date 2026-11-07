//! Settling a job after an attempt: delete it, give it back for a later
//! attempt with a growing delay, or fail it for good (its run first, when
//! the run can never continue either).

use std::time::Duration;

use linch_core::{RunError, RunId, SessionId, StoreError};

use crate::error::{WorkerError, chain};
use crate::execute::Shared;
use crate::lease::JobLease;
use crate::outcome::Outcome;

/// Failed attempts after which a job is failed for good. Waiting for a busy
/// session is not a failure. PgQueuer's default, which the Python worker
/// used.
pub(crate) const MAX_FAILURES: i32 = 5;

/// Longest wait before a job is tried again (PgQueuer's default).
pub(crate) const MAX_RETRY_DELAY: Duration = Duration::from_secs(300);

/// The wait after attempt `attempt` (1 for the first): `base`, doubled for
/// every earlier attempt, at most [`MAX_RETRY_DELAY`].
pub(crate) fn backoff(base: Duration, attempt: i32) -> Duration {
    let doublings = u32::try_from(attempt.saturating_sub(1))
        .unwrap_or(0)
        .min(20);
    base.saturating_mul(1 << doublings).min(MAX_RETRY_DELAY)
}

/// Settle `lease`'s job as `outcome` says.
pub(crate) async fn settle(shared: &Shared, lease: &JobLease, outcome: Outcome) {
    let (pool, id) = (&shared.pool, lease.job.id.0);
    let settled = match outcome {
        Outcome::Done => lease.ack(pool).await,
        Outcome::Retry(error) => retry(shared, lease, &error, true).await,
        Outcome::Busy(error) => {
            let delay = backoff(shared.retry_delay, lease.attempt());
            tracing::info!(job = id, %error, ?delay, "waiting for the session's run to end");
            lease.release(pool, delay, &error, false).await
        }
        Outcome::Fail(error) => {
            tracing::warn!(job = id, %error, "failing a job");
            lease.fail(pool, &error).await
        }
        // The run first: a crash in between leaves a terminal run, which the
        // job's next attempt acks.
        Outcome::FailRun { run_id, error } => {
            match fail_run(shared, lease, &run_id, &error).await {
                Ok(()) => {
                    tracing::warn!(job = id, run = %run_id, %error, "failing a job and its run");
                    lease.fail(pool, &error).await
                }
                Err(why) => retry(shared, lease, &why, false).await,
            }
        }
        Outcome::Fenced(error) => {
            tracing::info!(job = id, %error, "another owner claimed the run; dropping the job");
            lease.ack(pool).await
        }
        Outcome::Lost => {
            tracing::info!(job = id, "lost the job; dropped its run");
            Ok(true)
        }
    };
    match settled {
        Ok(true) => {}
        Ok(false) => tracing::info!(job = id, "the job moved to another worker"),
        // The job becomes visible again after its timeout.
        Err(error) => tracing::warn!(job = id, error = chain(&error), "settling a job failed"),
    }
}

/// Give the failed attempt's job back with a growing delay, or fail it for
/// good once it has failed [`MAX_FAILURES`] times. Then its run fails too
/// (when `with_run` and the job names one): nothing will continue it.
async fn retry(
    shared: &Shared,
    lease: &JobLease,
    error: &str,
    with_run: bool,
) -> Result<bool, WorkerError> {
    let (pool, id) = (&shared.pool, lease.job.id.0);
    let failures = lease.failures() + 1;
    if failures < MAX_FAILURES {
        let delay = backoff(shared.retry_delay, lease.attempt());
        tracing::warn!(job = id, %error, failures, ?delay, "retrying a job");
        return lease.release(pool, delay, error, true).await;
    }
    let error = format!("gave up after {failures} failed attempts: {error}");
    if let Some(run_id) = lease.job.run_id.as_ref().filter(|_| with_run) {
        match fail_run(shared, lease, run_id, &error).await {
            Ok(()) => tracing::warn!(job = id, run = %run_id, %error, "failing a job and its run"),
            Err(why) => {
                tracing::warn!(job = id, %error, run = %run_id, %why, "failing a job; failing its run failed")
            }
        }
    } else {
        tracing::warn!(job = id, %error, "failing a job");
    }
    lease.fail(pool, &error).await
}

/// Mark `run_id` failed with `error`; a run that is already terminal (or
/// was never created) is left as it is.
async fn fail_run(
    shared: &Shared,
    lease: &JobLease,
    run_id: &RunId,
    error: &str,
) -> Result<(), String> {
    let namespace = &lease.job.namespace;
    let Some(agent) = shared.agents.get(namespace) else {
        return Err(format!("no agent for namespace {namespace}"));
    };
    let options = shared.options(SessionId::new(""));
    match agent.fail(run_id.clone(), error, options).await {
        Ok(_) | Err(RunError::Terminal { .. }) => Ok(()),
        Err(RunError::Store(StoreError::NotFound { .. })) => Ok(()),
        Err(other) => Err(chain(&other)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_retry_delay_doubles_up_to_the_cap() {
        let base = Duration::from_millis(10);
        let delays: Vec<u128> = (1..=4).map(|n| backoff(base, n).as_millis()).collect();
        assert_eq!(delays, [10, 20, 40, 80]);
        assert_eq!(backoff(base, 0), base);
        assert_eq!(backoff(Duration::from_secs(5), 7), MAX_RETRY_DELAY);
        assert_eq!(backoff(Duration::from_secs(5), i32::MAX), MAX_RETRY_DELAY);
    }
}
