//! Running one dequeued job: pick `run`, `resume` or `approve` from the
//! run's durable state, drain the run stream while the heartbeat holds the
//! job, then settle the job.
//!
//! Losing the job (another worker dequeued it, or no heartbeat landed for a
//! whole visibility timeout) drops the run stream. Dropping is a crash for
//! the run: it stays resumable, and the next worker's claim fences this one
//! out by epoch. The run's cancel token is never used for this, because a
//! cancel ends the run as `aborted`.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use futures::StreamExt;
use linch_core::{Agent, Phase, RunError, RunOptions, RunRecord, RunStore, RunStream};
use linch_core::{RunId, SessionId, StoreError};
use linch_postgres::{PgPool, PgRunStore};
use tokio::time::Instant;

use crate::error::chain;
use crate::lease::{Job, JobLease};

/// What every job task of one worker shares.
pub(crate) struct Shared {
    pub(crate) pool: PgPool,
    pub(crate) runs: PgRunStore,
    pub(crate) agents: HashMap<String, Agent>,
    pub(crate) owner: String,
    pub(crate) timeout: Duration,
    pub(crate) retry_delay: Duration,
}

impl Shared {
    fn options(&self, session_id: SessionId) -> RunOptions {
        let mut options = RunOptions::new(session_id);
        options.owner = self.owner.clone();
        options
    }
}

/// How an attempt at a job ended.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    /// The run is terminal or waits for approval: delete the job.
    Done,
    /// Try again after the retry delay.
    Retry(String),
    /// The job can never succeed: keep it, failed, with this error.
    Fail(String),
    /// The run can never continue either: mark it failed, then the job.
    FailRun {
        /// The run to fail.
        run_id: RunId,
        /// Why; the run's `Error` event and the job's error.
        error: String,
    },
    /// Another worker holds the job now: leave it alone.
    Lost,
}

/// Map an error of `run_id` onto the job's outcome.
pub(crate) fn classify(error: &RunError, run_id: &RunId) -> Outcome {
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
        // Includes a lost run lease and a session append conflict.
        RunError::Store(_) | RunError::Journal(_) => Outcome::Retry(chain(error)),
    }
}

/// Run the job and settle it; the task body of one dequeued job.
pub(crate) async fn execute(shared: Arc<Shared>, lease: JobLease) {
    let outcome = tokio::select! {
        outcome = attempt(&shared, &lease) => outcome,
        () = keep_alive(&shared, &lease) => Outcome::Lost,
    };
    let (pool, id) = (&shared.pool, lease.job.id.0);
    let settled = match &outcome {
        Outcome::Done => lease.ack(pool).await,
        Outcome::Retry(error) => {
            tracing::warn!(job = id, %error, "retrying a job");
            lease.release(pool, shared.retry_delay, error).await
        }
        Outcome::Fail(error) => {
            tracing::warn!(job = id, %error, "failing a job");
            lease.fail(pool, error).await
        }
        // The run first: a crash in between leaves a terminal run, which the
        // job's next attempt acks.
        Outcome::FailRun { run_id, error } => {
            match fail_run(&shared, &lease.job, run_id, error).await {
                Ok(()) => {
                    tracing::warn!(job = id, run = %run_id, %error, "failing a job and its run");
                    lease.fail(pool, error).await
                }
                Err(why) => {
                    tracing::warn!(job = id, error = %why, "failing a run failed; retrying the job");
                    lease.release(pool, shared.retry_delay, &why).await
                }
            }
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

async fn attempt(shared: &Shared, lease: &JobLease) -> Outcome {
    let job = &lease.job;
    let Some(agent) = shared.agents.get(&job.namespace) else {
        let error = format!("no agent for namespace {}", job.namespace);
        return Outcome::Retry(error);
    };
    let mut stream = match open(shared, lease, agent).await {
        Ok(Some(stream)) => stream,
        Ok(None) => return Outcome::Done,
        Err(outcome) => return outcome,
    };
    // The stream ends after `Result` or `Suspended`; both end the job.
    while let Some(item) = stream.next().await {
        if let Err(error) = item {
            return classify(&error, stream.run_id());
        }
    }
    Outcome::Done
}

/// Mark `run_id` failed with `error`; a run that is already terminal (or
/// was never created) is left as it is.
async fn fail_run(shared: &Shared, job: &Job, run_id: &RunId, error: &str) -> Result<(), String> {
    let Some(agent) = shared.agents.get(&job.namespace) else {
        return Err(format!("no agent for namespace {}", job.namespace));
    };
    let options = shared.options(SessionId::new(""));
    match agent.fail(run_id.clone(), error, options).await {
        Ok(_) | Err(RunError::Terminal { .. }) => Ok(()),
        Err(RunError::Store(StoreError::NotFound { .. })) => Ok(()),
        Err(other) => Err(chain(&other)),
    }
}

/// The stream to drain, `None` when there is nothing to run.
async fn open(
    shared: &Shared,
    lease: &JobLease,
    agent: &Agent,
) -> Result<Option<RunStream>, Outcome> {
    let job = &lease.job;
    if let Some(run_id) = &job.run_id {
        match shared.runs.load(&job.namespace, run_id).await {
            Ok(record) => return continue_run(shared, agent, job, record),
            // A start job recorded this id, but the run's create never landed.
            Err(StoreError::NotFound { .. }) if job.start.is_some() => {}
            Err(StoreError::NotFound { .. }) => {
                return Err(Outcome::Fail(format!("run {run_id} not found")));
            }
            Err(error) => return Err(Outcome::Retry(chain(&error))),
        }
    }
    let Some((session_id, prompt)) = &job.start else {
        return Err(Outcome::Fail(
            "the job names neither a run nor a prompt".into(),
        ));
    };
    let stream = agent.run(prompt.clone(), shared.options(session_id.clone()));
    // Nothing is written until the stream is polled, so the job knows its
    // run before the run exists.
    match lease.record_run(&shared.pool, stream.run_id()).await {
        Ok(true) => Ok(Some(stream)),
        Ok(false) => Err(Outcome::Lost),
        Err(error) => Err(Outcome::Retry(chain(&error))),
    }
}

/// Resume or approve an existing run, judged by its durable checkpoint.
fn continue_run(
    shared: &Shared,
    agent: &Agent,
    job: &Job,
    record: RunRecord,
) -> Result<Option<RunStream>, Outcome> {
    if record.status.is_terminal() {
        return Ok(None);
    }
    // "Suspended" is the checkpoint phase, not the status: an approve that
    // crashed between its claim and its commit leaves the status `running`.
    let suspended = matches!(record.checkpoint.phase, Phase::AwaitingApproval { .. });
    let options = shared.options(record.session_id);
    match (&job.approvals, suspended) {
        (Some(Err(error)), _) => Err(Outcome::Fail(format!("approvals do not decode: {error}"))),
        (Some(Ok(approvals)), true) => {
            let answers = approvals.clone().into_iter().collect();
            Ok(Some(agent.approve(record.run_id, answers, options)))
        }
        // Waits for an approve job; a resume would only suspend it again.
        (None, true) => Ok(None),
        // A plain resume, or an approve whose answers already committed.
        (_, false) => Ok(Some(agent.resume(record.run_id, options))),
    }
}

/// Extend the job's visibility every third of the timeout. Returns once the
/// job is lost: another worker holds it, or no extension landed within a
/// whole timeout of the last confirmed one.
async fn keep_alive(shared: &Shared, lease: &JobLease) {
    let timeout = shared.timeout;
    let mut confirmed = lease.since;
    loop {
        tokio::time::sleep(timeout / 3).await;
        let sent = Instant::now();
        let remaining = timeout.saturating_sub(confirmed.elapsed());
        let beat = lease.heartbeat(&shared.pool, timeout);
        match tokio::time::timeout(remaining, beat).await {
            Ok(Ok(true)) => confirmed = sent,
            Ok(Ok(false)) => return,
            Ok(Err(error)) => {
                let id = lease.job.id.0;
                tracing::warn!(job = id, error = chain(&error), "a heartbeat failed");
            }
            Err(_elapsed) => {}
        }
        if confirmed.elapsed() >= timeout {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use linch_core::{ReconcileError, RunStatus};

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
        let lost = RunError::Store(StoreError::LeaseLost {
            run_id: run(),
            epoch: 1,
        });
        assert!(matches!(classify(&lost), Outcome::Retry(text) if text.contains("lease")));
        let conflict = RunError::Store(StoreError::Conflict {
            message: "session".into(),
        });
        assert!(matches!(classify(&conflict), Outcome::Retry(_)));
        let reconcile = RunError::Reconcile(ReconcileError::MissingToolCalls);
        assert!(fails_run(classify(&reconcile)));
    }
}
