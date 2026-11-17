//! Steering a run: [`Agent::steer`], and the write it makes for a run that
//! no runner of this process drives.

use std::sync::Arc;

use tokio::sync::oneshot;

use super::{Agent, RunError};
use crate::events::Event;
use crate::events::kinds::SteerReceivedEvent;
use crate::ids::RunId;
use crate::journal::Journal;
use crate::run::RunStatus;
use crate::runner::{Arrival, steers_of};
use crate::store::StoreError;

/// Longest steer text, in bytes.
pub const MAX_STEER_BYTES: usize = 16 * 1024;

/// Most steers a run holds that it has not delivered yet.
pub const MAX_PENDING_STEERS: usize = 16;

/// Longest steer id, in bytes.
const MAX_ID_BYTES: usize = 128;

impl Agent {
    /// Send `text` to the run `run_id`; it reads it as a user message at its
    /// next turn boundary.
    ///
    /// The steer is a `steer_received` event in the run's own journal,
    /// durable before this returns. A run this process drives writes it
    /// itself, at its next step, so the answer waits for the model call or
    /// tool batch in progress. A run that waits for an approval or an answer
    /// takes it too, written under a claim of the run that leaves its
    /// suspension as it was; the run delivers it once it continues. The run
    /// logs it after the tool results of the batch it is in, before the next
    /// provider call, or, when the model answered without tools, before the
    /// run would end, which then takes another turn. A resumed run reads
    /// what is pending back from its journal: a steer is logged once.
    ///
    /// On the memory stores nothing survives the process.
    ///
    /// Args:
    ///     run_id: A run of this agent's namespace that is not finished.
    ///     text: What the model reads; not blank, at most
    ///         [`MAX_STEER_BYTES`] bytes.
    ///     id: Names the steer, so a call sent again queues it once; `None`
    ///         takes a fresh one. At most 128 bytes of letters, digits and
    ///         `-_.:`.
    ///
    /// Returns:
    ///     The steer's id, which the `alignment` user event that delivers it
    ///     names in `steer_id`.
    ///
    /// Raises:
    ///     RunError: `InvalidSteer` for bad text or id; `Store` (`NotFound`)
    ///         for an unknown run, (`RunLeased`) when another process drives
    ///         it, or when a read or write fails; `Terminal` for a finished
    ///         run; `SteerQueueFull` at [`MAX_PENDING_STEERS`] undelivered
    ///         steers; `Journal` when the write failed; `Store` (`Conflict`)
    ///         when the run stopped before it took the steer (send it again
    ///         with the same id).
    pub async fn steer(
        &self,
        run_id: RunId,
        text: impl Into<String>,
        id: Option<String>,
    ) -> Result<String, RunError> {
        let text = text.into();
        let invalid = |reason: &str| RunError::InvalidSteer {
            run_id: run_id.clone(),
            reason: reason.to_owned(),
        };
        if text.trim().is_empty() {
            return Err(invalid("the text is empty"));
        }
        if text.len() > MAX_STEER_BYTES {
            let reason = format!("the text is longer than {MAX_STEER_BYTES} bytes");
            return Err(invalid(&reason));
        }
        let id = match id {
            Some(id) => {
                let allowed = |c: char| c.is_ascii_alphanumeric() || "-_.:".contains(c);
                if id.is_empty() || id.len() > MAX_ID_BYTES || !id.chars().all(allowed) {
                    return Err(invalid("the id is empty, too long or has other characters"));
                }
                id
            }
            None => uuid::Uuid::new_v4().simple().to_string(),
        };
        let inner = &self.inner;
        let record = inner.runs.load(&inner.namespace, &run_id).await?;
        if record.status.is_terminal() {
            let status = record.status;
            return Err(RunError::Terminal { run_id, status });
        }
        let (reply, answer) = oneshot::channel();
        let arrival = Arrival {
            id: id.clone(),
            text,
            reply,
        };
        match inner.steering.send(&run_id, arrival) {
            Ok(()) => match answer.await {
                Ok(answered) => answered?,
                Err(_) => return Err(self.stopped(run_id).await),
            },
            Err(arrival) => self.steer_stored(run_id, arrival, record.status).await?,
        }
        Ok(id)
    }

    /// Why a run that took a steer did not answer: it ended, or it failed
    /// first.
    async fn stopped(&self, run_id: RunId) -> RunError {
        let inner = &self.inner;
        match inner.runs.load(&inner.namespace, &run_id).await {
            Ok(record) if record.status.is_terminal() => {
                let status = record.status;
                RunError::Terminal { run_id, status }
            }
            Ok(_) => RunError::Store(StoreError::Conflict {
                message: format!(
                    "run {run_id} stopped before it took the steer; resume it and send the \
                     steer again with the same id"
                ),
            }),
            Err(error) => error.into(),
        }
    }

    /// Write the steer into the journal of a run no runner of this process
    /// drives, under a claim of the run: another process that drives it
    /// refuses the claim (`RunLeased`). A claim turns a suspended run
    /// `running`, so the write puts it back in the suspension it was in,
    /// whatever it decided.
    async fn steer_stored(
        &self,
        run_id: RunId,
        arrival: Arrival,
        before: RunStatus,
    ) -> Result<(), RunError> {
        let inner = &self.inner;
        let (runs, namespace) = (&inner.runs, inner.namespace.as_str());
        let claimed = runs.claim(namespace, &run_id, "linch-steer").await;
        let lease = match claimed {
            Ok(lease) => lease,
            Err(StoreError::InvalidTransition { from, .. }) if from.is_terminal() => {
                return Err(RunError::Terminal {
                    run_id,
                    status: from,
                });
            }
            Err(error) => return Err(error.into()),
        };
        let decided = async {
            let record = runs.load(namespace, &run_id).await?;
            if record.status.is_terminal() {
                let status = record.status;
                return Err(RunError::Terminal { run_id, status });
            }
            let events = runs.events(namespace, &run_id, 0).await?;
            let (received, pending) = steers_of(&events);
            let decision = if received.contains(&arrival.id) {
                Decision::Skip
            } else if pending.len() >= MAX_PENDING_STEERS {
                let max = MAX_PENDING_STEERS;
                Decision::Refuse(RunError::SteerQueueFull { run_id, max })
            } else {
                Decision::Stage
            };
            Ok((record.last_seq, decision))
        }
        .await;
        let written = match decided {
            Err(error) => Err(error),
            Ok((last_seq, decision)) => {
                let mut journal = Journal::new(Arc::clone(runs), lease.clone(), last_seq);
                let stage = matches!(decision, Decision::Stage);
                if stage {
                    journal.stage(Event::SteerReceived(SteerReceivedEvent {
                        steer_id: arrival.id.clone(),
                        text: arrival.text.clone(),
                    }));
                }
                let flushed = match (before.is_awaiting(), stage) {
                    (true, _) => journal.flush_as(before).await.map(drop),
                    (false, true) => journal.flush().await.map(drop),
                    (false, false) => Ok(()),
                };
                match (flushed, decision) {
                    (Err(error), _) => Err(error.into()),
                    (Ok(()), Decision::Refuse(error)) => Err(error),
                    (Ok(()), _) => Ok(()),
                }
            }
        };
        // A failed release leaves the claim to expire (a durable store).
        let _ = runs.release(&lease).await;
        written
    }
}

/// What a steer sent to a run nobody drives comes to.
enum Decision {
    /// Write it.
    Stage,
    /// It is in the journal already.
    Skip,
    /// Refuse it.
    Refuse(RunError),
}
