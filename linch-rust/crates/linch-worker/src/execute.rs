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
use linch_core::{Agent, Phase, RunOptions, RunRecord, RunStore, RunStream};
use linch_core::{SessionId, StoreError};
use linch_postgres::{PgPool, PgRunStore};
use tokio::time::Instant;

use crate::error::chain;
use crate::lease::JobLease;
use crate::outcome::{Outcome, classify};
use crate::settle::settle;

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
    pub(crate) fn options(&self, session_id: SessionId) -> RunOptions {
        let mut options = RunOptions::new(session_id);
        options.owner = self.owner.clone();
        options
    }
}

/// Run the job and settle it; the task body of one dequeued job.
pub(crate) async fn execute(shared: Arc<Shared>, lease: JobLease) {
    let outcome = tokio::select! {
        outcome = attempt(&shared, &lease) => outcome,
        () = keep_alive(&shared, &lease) => Outcome::Lost,
    };
    settle(&shared, &lease, outcome).await;
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

/// The stream to drain, `None` when there is nothing to run.
async fn open(
    shared: &Shared,
    lease: &JobLease,
    agent: &Agent,
) -> Result<Option<RunStream>, Outcome> {
    let job = &lease.job;
    if let Some(run_id) = &job.run_id {
        match shared.runs.load(&job.namespace, run_id).await {
            Ok(record) => return continue_run(shared, agent, lease, record),
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
    lease: &JobLease,
    record: RunRecord,
) -> Result<Option<RunStream>, Outcome> {
    if record.status.is_terminal() {
        return Ok(None);
    }
    // "Suspended" is the checkpoint phase, not the status: an approve that
    // crashed between its claim and its commit leaves the status `running`.
    let suspended = matches!(record.checkpoint.phase, Phase::AwaitingApproval { .. });
    let options = shared.options(record.session_id);
    let (job, run_id) = (&lease.job, record.run_id);
    match (&job.approvals, suspended) {
        (Some(Err(error)), _) => Err(Outcome::Fail(format!("approvals do not decode: {error}"))),
        // On a first attempt a run that is not suspended was never waiting
        // for these answers: core refuses the approve before it claims, so
        // whoever drives the run keeps it.
        (Some(Ok(approvals)), _) if suspended || lease.attempt() == 1 => {
            let answers = approvals.clone().into_iter().collect();
            Ok(Some(agent.approve(run_id, answers, options)))
        }
        // Waits for an approve job; a resume would only suspend it again.
        (None, true) => Ok(None),
        (None, false) => Ok(Some(agent.resume(run_id, options))),
        // An earlier attempt of this job may have committed the answers
        // and stopped: the run continues from them.
        (Some(Ok(_)), _) => {
            let attempt = lease.attempt();
            tracing::warn!(
                job = job.id.0,
                run = %run_id,
                attempt,
                "the run is no longer awaiting approval; continuing the approve job as a resume"
            );
            Ok(Some(agent.resume(run_id, options)))
        }
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
