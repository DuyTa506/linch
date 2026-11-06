//! Failing a run for good from outside its loop (see
//! [`crate::agent::Agent::fail`]).

use std::sync::Arc;
use std::time::Instant;

use serde_json::json;

use super::end::millis;
use crate::agent::{AgentInner, RunError, RunOptions};
use crate::events::kinds::{ErrorEvent, ResultEvent, ResultSubtype};
use crate::events::{Envelope, Event};
use crate::ids::RunId;
use crate::journal::Journal;
use crate::run::{Phase, RunStatus, StopReason};
use crate::store::StoreError;

/// Claim `run_id`, then commit an `Error` holding `reason`, the `Result`
/// and status `failed` in one write.
pub(crate) async fn fail(
    agent: &AgentInner,
    run_id: RunId,
    reason: String,
    options: RunOptions,
) -> Result<Vec<Envelope>, RunError> {
    let started = Instant::now();
    let (runs, namespace) = (&agent.runs, agent.namespace.as_str());
    let lease = match runs.claim(namespace, &run_id, &options.owner).await {
        Ok(lease) => lease,
        Err(StoreError::InvalidTransition { from, .. }) if from.is_terminal() => {
            return Err(RunError::Terminal {
                run_id,
                status: from,
            });
        }
        Err(error) => return Err(error.into()),
    };
    // The claim fixed the state: no other owner can commit any more.
    let record = runs.load(namespace, &run_id).await?;
    let checkpoint = record.checkpoint;
    // A call dispatched in these phases may have been paid for, but its
    // receipt never landed: its usage is unknown.
    let unsettled = matches!(
        checkpoint.phase,
        Phase::ProviderPending { .. } | Phase::CompactionPending { .. }
    );
    let mut journal = Journal::new(Arc::clone(runs), lease, record.last_seq);
    let error = json!({"name": "RunFailed", "message": reason, "retryable": false});
    let error = error.as_object().cloned().unwrap_or_default();
    journal.stage(Event::Error(ErrorEvent { error }));
    let result = ResultEvent {
        subtype: ResultSubtype::Error,
        stop_reason: StopReason::Error,
        total_usage: checkpoint.usage,
        usage_complete: checkpoint.usage_complete && !unsettled,
        duration_ms: millis(started.elapsed()),
        total_cost_usd: checkpoint.cost_usd,
        final_text: None,
        structured_output: None,
        structured_error: None,
        trace_id: None,
    };
    Ok(journal.finish(RunStatus::Failed, result).await?)
}
