//! Durable run and session storage.
//!
//! Records are keyed by a host-chosen namespace plus an id, so tenants never
//! share a key space. The in-memory implementations are for tests and
//! development; linch-postgres implements the same traits and must pass the
//! same conformance suite.
//!
//! [`RunStore::commit`] is the only write to a running run, so every store
//! write is a group-commit barrier by construction.
//!
//! [`TaskStore`] keeps the session tasks; the session stores implement it.
//!
//! A store whose claims expire ([`RunStore::lease_ttl`]) also keeps two
//! processes from driving one run at once: a claim is refused while
//! another holder keeps its lease alive, and the run's driver renews the
//! lease on a timer. The memory stores are for one process and never
//! refuse a claim.

use std::time::Duration;

use async_trait::async_trait;
use thiserror::Error;

use crate::canonical::Digest;
use crate::events::Envelope;
use crate::ids::{RunId, SessionId};
use crate::run::{Checkpoint, RunStatus};
use crate::session_log::LogEntry;

pub mod conformance;
pub mod faulty;
pub mod memory;
mod tasks;

pub use tasks::{NewTask, Task, TaskPatch, TaskStatus, TaskStore};

/// Everything written when a run is created, in one write.
#[derive(Debug, Clone)]
pub struct NewRun {
    /// Host-chosen key space.
    pub namespace: String,
    /// New run id.
    pub run_id: RunId,
    /// Session the run belongs to.
    pub session_id: SessionId,
    /// Parent run, for a child run.
    pub parent_run_id: Option<RunId>,
    /// Digest of the run contract (spec, tools, hook policies).
    pub contract: Digest,
    /// Initial checkpoint (phase `started`).
    pub checkpoint: Checkpoint,
    /// Initial events, with contiguous seqs from 1.
    pub events: Vec<Envelope>,
}

/// A stored run.
#[derive(Debug, Clone)]
pub struct RunRecord {
    /// Key space.
    pub namespace: String,
    /// Run id.
    pub run_id: RunId,
    /// Owning session.
    pub session_id: SessionId,
    /// Parent run, for a child run.
    pub parent_run_id: Option<RunId>,
    /// Contract digest written at creation.
    pub contract: Digest,
    /// Current status.
    pub status: RunStatus,
    /// Lease epoch; each claim increments it.
    pub epoch: u64,
    /// Latest committed checkpoint.
    pub checkpoint: Checkpoint,
    /// Seq of the last committed event (0 when none).
    pub last_seq: u64,
}

/// Proof of ownership for writes to one run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lease {
    /// Key space.
    pub namespace: String,
    /// Leased run.
    pub run_id: RunId,
    /// Owner name, for diagnostics.
    pub owner: String,
    /// Epoch at claim time. A later claim fences this lease out.
    pub epoch: u64,
}

/// One atomic write to a running run.
#[derive(Debug, Clone, Default)]
pub struct Commit {
    /// Events to append; their seqs must continue `last_seq` without gaps.
    pub events: Vec<Envelope>,
    /// New checkpoint, if it changed.
    pub checkpoint: Option<Checkpoint>,
    /// New status, if it changed.
    pub status: Option<RunStatus>,
}

impl Commit {
    /// Whether the commit writes nothing (callers skip the store call).
    pub fn is_empty(&self) -> bool {
        self.events.is_empty() && self.checkpoint.is_none() && self.status.is_none()
    }
}

/// A store operation failed.
#[derive(Debug, Error)]
pub enum StoreError {
    /// No record with that key.
    #[error("{kind} {id} not found in namespace {namespace}")]
    NotFound {
        /// `run` or `session`.
        kind: &'static str,
        /// Key space searched.
        namespace: String,
        /// Missing id.
        id: String,
    },
    /// The lease was fenced out by a newer claim.
    #[error("lease on run {run_id} lost (epoch {epoch})")]
    LeaseLost {
        /// Run whose lease was lost.
        run_id: RunId,
        /// Epoch of the stale lease.
        epoch: u64,
    },
    /// The record already exists, or a session append found a different
    /// length than expected.
    #[error("conflict: {message}")]
    Conflict {
        /// What conflicted.
        message: String,
    },
    /// A run create named a session that already has a run that is not
    /// terminal; a session holds at most one per namespace. Resume, approve
    /// or fail `run_id` first.
    #[error("session {session_id} is busy with run {run_id}")]
    SessionBusy {
        /// The busy session.
        session_id: SessionId,
        /// Its run that is not terminal.
        run_id: RunId,
    },
    /// Another holder (another process) drives the run and keeps its lease
    /// alive; nothing was written. Try again once that holder stops: at
    /// the latest when its lease runs out, `expires_in` from now.
    #[error("run {run_id} is held by {owner:?} for up to {expires_in:?} more")]
    RunLeased {
        /// The run that was asked for.
        run_id: RunId,
        /// Owner name the holder claimed under.
        owner: String,
        /// How long the holder's lease runs without another renewal.
        expires_in: Duration,
    },
    /// Event seqs did not continue the stored sequence.
    #[error("event seq gap: expected {expected}, got {got}")]
    SeqGap {
        /// Next seq the store expected.
        expected: u64,
        /// Seq that was written.
        got: u64,
    },
    /// A status change that the run lifecycle does not allow.
    #[error("invalid run status transition {from:?} -> {to:?}")]
    InvalidTransition {
        /// Current status.
        from: RunStatus,
        /// Requested status.
        to: RunStatus,
    },
    /// The storage backend failed.
    #[error("store backend error")]
    Backend(#[source] Box<dyn std::error::Error + Send + Sync>),
}

/// Durable run state: records, checkpoints and the event journal.
#[async_trait]
pub trait RunStore: Send + Sync {
    /// Create a run with its first checkpoint and events, in one write.
    ///
    /// Refuses, writing nothing, with `Conflict` when the run id exists and
    /// with [`StoreError::SessionBusy`] when the namespace already holds a
    /// run of the same session that is not terminal. The check and the
    /// insert are one atomic step, so of two creates racing on one session
    /// exactly one lands.
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError>;

    /// Take ownership of a run, fencing out any earlier lease. A suspended
    /// (`awaiting_*`) run becomes `running` only through a claim.
    ///
    /// A store with a [`RunStore::lease_ttl`] refuses, writing nothing,
    /// with [`StoreError::RunLeased`] while the run is `running` under an
    /// unexpired lease another holder took; a claim through the holder that
    /// holds it fences that lease out as before.
    async fn claim(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError>;

    /// Like [`RunStore::claim`], but also over a live lease of another
    /// holder, which is fenced out. For failing a run for good and for a
    /// parent taking its child runs back.
    async fn seize(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError> {
        self.claim(namespace, run_id, owner).await
    }

    /// How long a claim holds without a renewal; `None` when claims never
    /// expire and are never refused (the memory stores). A run's driver
    /// renews its lease every third of it while the run is polled.
    ///
    /// A wrapper store must forward this, [`RunStore::renew`],
    /// [`RunStore::release`] and [`RunStore::seize`], or the runs behind it
    /// lose their leases.
    fn lease_ttl(&self) -> Option<Duration> {
        None
    }

    /// Extend `lease` by a whole [`RunStore::lease_ttl`] from now.
    ///
    /// Raises:
    ///     StoreError: `LeaseLost` once a later claim moved the epoch.
    async fn renew(&self, lease: &Lease) -> Result<(), StoreError> {
        let _ = lease;
        Ok(())
    }

    /// End `lease` early, so another holder may claim the run at once. A
    /// lease a later claim fenced out is left alone.
    async fn release(&self, lease: &Lease) -> Result<(), StoreError> {
        let _ = lease;
        Ok(())
    }

    /// Read the current record.
    async fn load(&self, namespace: &str, run_id: &RunId) -> Result<RunRecord, StoreError>;

    /// Apply one atomic write under `lease`. A terminal status is final.
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError>;

    /// Committed events with seq greater than `after_seq`, in order.
    async fn events(
        &self,
        namespace: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError>;
}

/// Durable session logs.
#[async_trait]
pub trait SessionStore: Send + Sync {
    /// Create an empty session. Fails with `Conflict` if it exists.
    async fn create(&self, namespace: &str, session_id: &SessionId) -> Result<(), StoreError>;

    /// Every logged entry, in order.
    async fn load(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<LogEntry>, StoreError>;

    /// Append `entries` if the log currently has `expected_len` entries;
    /// otherwise `Conflict`. Returns the new length.
    async fn append(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError>;
}
