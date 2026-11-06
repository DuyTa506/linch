//! Run event journal with group commit at barriers.
//!
//! A [`Journal`] holds one run's events and checkpoint in memory and writes
//! them in one [`RunStore::commit`] at each barrier, so a tool turn costs
//! `3 + 2k` run writes for `k` started tools instead of one write per step.
//!
//! - [`Journal::stage`] allocates the next seq to a durable event and holds
//!   it. A stream-only event is never stored: it comes back at once, with the
//!   last durable seq.
//! - [`Journal::save`] applies the phase rule of BLUEPRINT 13.1.3:
//!   `Started`, `AssistantReceived` and `Compacted` (a paid summary) write
//!   through, `AwaitingApproval` writes through with status
//!   `awaiting_approval`, and `ProviderPending`, `CompactionPending`,
//!   `ToolBatch` and `ToolResults` are held. A held checkpoint is a snapshot:
//!   it covers only the events pending when it was saved.
//! - The caller places the barriers: [`Journal::flush`] before a provider
//!   dispatch, after a summary call that gave no summary, and around every
//!   tool that really runs (its start and its end), and
//!   [`Journal::flush_checkpoint`] before every session write.
//!   Whether a tool runs is the scheduler's knowledge, not the journal's.
//! - [`Journal::finish`] commits the held state, then the terminal status
//!   with the `Result` event; with nothing held, the pending events ride the
//!   terminal write.
//!
//! Every write returns the envelopes that just became durable and are meant
//! for the caller (`Persistence::Both`), in seq order, so nothing reaches the
//! caller before its barrier. Every committed state is one that writing each
//! step through at once would also have produced.
//!
//! A failed write poisons the journal: every later write fails with
//! [`JournalError::Poisoned`] and nothing more reaches the store. The run
//! keeps its last durable state, and [`reconcile`] recovers it on resume.

use std::mem;
use std::sync::Arc;

use thiserror::Error;

use crate::canonical::Digest;
use crate::events::kinds::ResultEvent;
use crate::events::{Envelope, Event, Persistence, WIRE_VERSION, now_ms};
use crate::ids::{RunId, SessionId};
use crate::run::{Checkpoint, Phase, RunStatus};
use crate::store::{Commit, Lease, NewRun, RunStore, StoreError};

mod reconcile;
#[cfg(test)]
mod tests;

pub use reconcile::{INTERRUPTED_TOOL_TEXT, ReconcileError, Recovery, ToolRecovery, reconcile};

/// A journal write failed or was refused.
#[derive(Debug, Error)]
pub enum JournalError {
    /// The store rejected or failed the write. The journal is now poisoned.
    #[error(transparent)]
    Store(#[from] StoreError),
    /// An earlier write failed, so this journal writes nothing more.
    #[error("an earlier journal write failed; resume the run to continue")]
    Poisoned,
    /// [`Journal::finish`] was called with a status that is not terminal.
    #[error("a run cannot finish with status {0:?}")]
    NotTerminal(RunStatus),
}

/// One run's write buffer: pending events, the held checkpoint, the lease.
pub struct Journal {
    store: Arc<dyn RunStore>,
    lease: Lease,
    durable_seq: u64,
    pending: Vec<Envelope>,
    held: Option<Held>,
    poisoned: bool,
}

/// A held checkpoint and how many pending events it covers.
struct Held {
    checkpoint: Checkpoint,
    covers: usize,
}

impl Journal {
    /// A journal that writes under `lease`.
    ///
    /// Args:
    ///     store: Store that receives every commit.
    ///     lease: Lease from `claim`. For a run that does not exist yet, use
    ///         epoch 0 and call [`Journal::create`] first.
    ///     durable_seq: Seq of the run's last durable event (0 for a new run).
    pub fn new(store: Arc<dyn RunStore>, lease: Lease, durable_seq: u64) -> Self {
        Self {
            store,
            lease,
            durable_seq,
            pending: Vec::new(),
            held: None,
            poisoned: false,
        }
    }

    /// The lease this journal writes under.
    pub fn lease(&self) -> &Lease {
        &self.lease
    }

    /// Seq of the last durable event.
    pub fn durable_seq(&self) -> u64 {
        self.durable_seq
    }

    /// Seq of the last staged durable event, pending or not. A `ToolBatch`
    /// checkpoint records this as its `after_seq`.
    pub fn last_seq(&self) -> u64 {
        self.durable_seq + self.pending.len() as u64
    }

    /// Stage one event.
    ///
    /// Returns:
    ///     The envelope to deliver now for a stream-only event, carrying the
    ///     last durable seq; `None` for a durable event, which is held until
    ///     the next write.
    pub fn stage(&mut self, event: Event) -> Option<Envelope> {
        if event.persistence() == Persistence::StreamOnly {
            return Some(self.envelope(self.durable_seq, event));
        }
        let envelope = self.envelope(self.last_seq() + 1, event);
        self.pending.push(envelope);
        None
    }

    /// Create the run with the staged events and `checkpoint`, then claim it
    /// as the lease owner. This is the `Started` write-through; the journal
    /// must be new (`durable_seq` 0).
    ///
    /// Args:
    ///     session_id: Session the run belongs to.
    ///     parent_run_id: Parent run, for a child run.
    ///     contract: Digest of the run contract.
    ///     checkpoint: First checkpoint (phase `started`).
    ///
    /// Returns:
    ///     The staged envelopes meant for the caller, now durable.
    ///
    /// Raises:
    ///     JournalError: If the journal is poisoned, or `create` or `claim`
    ///         fails (which poisons it).
    pub async fn create(
        &mut self,
        session_id: SessionId,
        parent_run_id: Option<RunId>,
        contract: Digest,
        checkpoint: Checkpoint,
    ) -> Result<Vec<Envelope>, JournalError> {
        self.check()?;
        let events = mem::take(&mut self.pending);
        let ready = deliverable(&events);
        let count = events.len() as u64;
        let run = NewRun {
            namespace: self.lease.namespace.clone(),
            run_id: self.lease.run_id.clone(),
            session_id,
            parent_run_id,
            contract,
            checkpoint,
            events,
        };
        let store = Arc::clone(&self.store);
        let claimed = async {
            store.create(run).await?;
            let lease = &self.lease;
            store
                .claim(&lease.namespace, &lease.run_id, &lease.owner)
                .await
        }
        .await;
        self.lease = claimed.map_err(|error| self.poison(error))?;
        self.durable_seq += count;
        self.held = None;
        Ok(ready)
    }

    /// Save a checkpoint under its phase rule (see the module docs).
    ///
    /// Returns:
    ///     The envelopes that a write-through made durable; empty when held.
    ///
    /// Raises:
    ///     JournalError: If a write-through fails, or the journal is poisoned.
    pub async fn save(&mut self, checkpoint: Checkpoint) -> Result<Vec<Envelope>, JournalError> {
        let status = match checkpoint.phase {
            Phase::Started { .. } | Phase::AssistantReceived { .. } | Phase::Compacted { .. } => {
                None
            }
            Phase::AwaitingApproval { .. } => Some(RunStatus::AwaitingApproval),
            Phase::ProviderPending { .. }
            | Phase::CompactionPending { .. }
            | Phase::ToolBatch { .. }
            | Phase::ToolResults { .. } => {
                self.check()?;
                let covers = self.pending.len();
                self.held = Some(Held { checkpoint, covers });
                return Ok(Vec::new());
            }
        };
        self.commit(self.pending.len(), Some(checkpoint), status)
            .await
    }

    /// Barrier: commit every pending event and the held checkpoint in one
    /// write. Writes nothing when there is nothing to write.
    ///
    /// Raises:
    ///     JournalError: If the write fails, or the journal is poisoned.
    pub async fn flush(&mut self) -> Result<Vec<Envelope>, JournalError> {
        let checkpoint = self.held.take().map(|held| held.checkpoint);
        self.commit(self.pending.len(), checkpoint, None).await
    }

    /// Session-write barrier: commit the held checkpoint with the events it
    /// covers. Events staged after it stay pending. Writes nothing when no
    /// checkpoint is held.
    ///
    /// Raises:
    ///     JournalError: If the write fails, or the journal is poisoned.
    pub async fn flush_checkpoint(&mut self) -> Result<Vec<Envelope>, JournalError> {
        match self.held.take() {
            Some(held) => self.commit(held.covers, Some(held.checkpoint), None).await,
            None => self.check().map(|()| Vec::new()),
        }
    }

    /// End the run: commit the held state, then `status` with `result`.
    ///
    /// With no checkpoint held, the pending events, the `Result` and the
    /// status go in one write, so a crash never leaves them half durable
    /// (a failed receipt with its `Error` but a running status).
    ///
    /// Raises:
    ///     JournalError: If `status` is not terminal, a write fails, or the
    ///         journal is poisoned.
    pub async fn finish(
        &mut self,
        status: RunStatus,
        result: ResultEvent,
    ) -> Result<Vec<Envelope>, JournalError> {
        if !status.is_terminal() {
            return Err(JournalError::NotTerminal(status));
        }
        let mut ready = match self.held {
            Some(_) => self.flush().await?,
            None => Vec::new(),
        };
        self.stage(Event::Result(result));
        ready.extend(self.commit(self.pending.len(), None, Some(status)).await?);
        Ok(ready)
    }

    /// Commit the first `count` pending events with `checkpoint` and
    /// `status`. A commit always supersedes the held checkpoint.
    async fn commit(
        &mut self,
        count: usize,
        checkpoint: Option<Checkpoint>,
        status: Option<RunStatus>,
    ) -> Result<Vec<Envelope>, JournalError> {
        self.check()?;
        let events: Vec<Envelope> = self.pending.drain(..count).collect();
        let ready = deliverable(&events);
        let commit = Commit {
            events,
            checkpoint,
            status,
        };
        self.held = None;
        if commit.is_empty() {
            return Ok(ready);
        }
        let result = self.store.commit(&self.lease, commit).await;
        result.map_err(|error| self.poison(error))?;
        self.durable_seq += count as u64;
        Ok(ready)
    }

    fn check(&self) -> Result<(), JournalError> {
        if self.poisoned {
            return Err(JournalError::Poisoned);
        }
        Ok(())
    }

    fn poison(&mut self, error: StoreError) -> JournalError {
        self.poisoned = true;
        JournalError::Store(error)
    }

    fn envelope(&self, seq: u64, event: Event) -> Envelope {
        Envelope {
            v: WIRE_VERSION,
            seq,
            run_id: self.lease.run_id.clone(),
            ts_ms: now_ms(),
            ignorable: false,
            event,
        }
    }
}

/// The envelopes of `events` that the caller sees once they are durable.
fn deliverable(events: &[Envelope]) -> Vec<Envelope> {
    events
        .iter()
        .filter(|envelope| envelope.event.persistence() == Persistence::Both)
        .cloned()
        .collect()
}
