//! Keeping a claimed run's lease alive while the run is driven.
//!
//! A store whose claims expire ([`RunStore::lease_ttl`]) refuses a claim
//! from another holder while the lease is alive. [`Held`] renews it every
//! third of the TTL, on a timer, so a long model call, a tool or a retry
//! back-off never lets it lapse. The heartbeat is a task of its own, tied to
//! the run: it beats while the run future lives, whether or not the caller
//! polls the stream, so a consumer that stops reading (a client whose
//! socket is full) does not lose the run to another process, which would
//! pay for the model call again. A host that stops (a crash, a frozen
//! process) stops its heartbeat with it and lets the lease run out. A beat
//! begins a third of the TTL after the one before it began, and gives up on
//! a renewal that outlasts that third, so one stuck renewal costs one beat.
//!
//! A renewal that finds the epoch moved (another holder seized the run)
//! ends the run at once with `LeaseLost`, as a fenced write would, unless
//! the run already suspended or ended. A run that stops without suspending
//! or ending (an error, a dropped stream) releases its lease, so another
//! process need not wait out the TTL; the agent keeps each such release in
//! its [`Releases`], so a host can wait for them before it exits. With a
//! store that has no TTL (the memory stores) nothing of this runs.

use std::future::{Future, pending};
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use futures::future::{Either, select};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use crate::agent::{AgentInner, RunError};
use crate::journal::{Journal, JournalError};
use crate::store::{Lease, RunStore, StoreError};

/// The lease releases of an agent's dropped runs still in flight, shared
/// with its subagents' agents.
#[derive(Clone, Default)]
pub(crate) struct Releases(Arc<Mutex<Vec<JoinHandle<()>>>>);

impl Releases {
    fn push(&self, release: JoinHandle<()>) {
        let mut releases = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        releases.retain(|release| !release.is_finished());
        releases.push(release);
    }

    /// Wait for every release started so far.
    pub(crate) async fn wait(&self) {
        let releases = std::mem::take(&mut *self.0.lock().unwrap_or_else(PoisonError::into_inner));
        for release in releases {
            let _ = release.await;
        }
    }
}

/// The heartbeat task of a run; it ends with its owner.
struct Beat(JoinHandle<StoreError>);

impl Beat {
    /// Stop the task and wait until it is gone, so no renewal of it is
    /// still in flight.
    async fn stop(&mut self) {
        self.0.abort();
        let _ = (&mut self.0).await;
    }
}

impl Drop for Beat {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// The lease of one claimed run: renewed while the run is driven, released
/// when the run stops without settling.
pub(super) struct Held {
    releases: Releases,
    store: Arc<dyn RunStore>,
    lease: Lease,
    /// The store's TTL; `None` when claims never expire.
    ttl: Option<Duration>,
    /// The journal's flag: the run suspended or ended.
    settled: Option<Arc<AtomicBool>>,
    /// The run future returned `Ok`, or the lease is gone: nothing to release.
    done: bool,
    /// The heartbeat, from the first [`Self::drive`].
    beat: Option<Beat>,
}

impl Held {
    /// The lease `agent` just claimed on its run store.
    pub(super) fn new(agent: &AgentInner, lease: &Lease) -> Self {
        Self {
            releases: agent.releases.clone(),
            store: Arc::clone(&agent.runs),
            lease: lease.clone(),
            ttl: agent.runs.lease_ttl(),
            settled: None,
            done: false,
            beat: None,
        }
    }

    /// Watch `journal`, which writes under this lease, for the run settling.
    pub(super) fn watch(&mut self, journal: &Journal) {
        self.settled = Some(journal.settled());
    }

    /// Drive `run` with the lease renewed beside it, by a task of its own.
    ///
    /// Returns:
    ///     What `run` returned, or a `LeaseLost` journal error once a
    ///     renewal found the run claimed by another holder (or the
    ///     heartbeat task itself failed).
    pub(super) async fn drive(
        &mut self,
        run: impl Future<Output = Result<(), RunError>>,
    ) -> Result<(), RunError> {
        let Some(ttl) = self.ttl else {
            return run.await;
        };
        let (store, lease) = (Arc::clone(&self.store), self.lease.clone());
        let beating = tokio::spawn(beat(store, lease, ttl, self.settled.clone()));
        let heartbeat = &mut self.beat.insert(Beat(beating)).0;
        let result = match select(pin!(run), heartbeat).await {
            Either::Left((result, _)) => result,
            Either::Right((lost, _)) => {
                // A task that died (a panic) cannot keep the lease.
                let lost = lost.unwrap_or_else(|_| StoreError::LeaseLost {
                    run_id: self.lease.run_id.clone(),
                    epoch: self.lease.epoch,
                });
                Err(RunError::Journal(JournalError::Store(lost)))
            }
        };
        if let Some(heartbeat) = &self.beat {
            heartbeat.0.abort();
        }
        // A lost lease is not ours to release.
        self.done = result.is_ok() || lost(&result);
        result
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        let mut beat = self.beat.take();
        let settled = (self.settled.as_ref()).is_some_and(|flag| flag.load(Ordering::SeqCst));
        if self.ttl.is_none() || self.done || settled {
            return;
        }
        // Dropped mid-run or stopped by an error: let another process take
        // the run now rather than after the TTL. Best effort: a release
        // that never lands just leaves the lease to expire.
        let Ok(runtime) = tokio::runtime::Handle::try_current() else {
            return;
        };
        let (store, lease) = (Arc::clone(&self.store), self.lease.clone());
        self.releases.push(runtime.spawn(async move {
            // The heartbeat first, so a renewal cannot land after the
            // release and hold the lease for another TTL.
            if let Some(beat) = &mut beat {
                beat.stop().await;
            }
            let _ = store.release(&lease).await;
        }));
    }
}

/// Whether `result` is a lost lease, which leaves nothing to release.
fn lost(result: &Result<(), RunError>) -> bool {
    matches!(
        result,
        Err(RunError::Store(StoreError::LeaseLost { .. })
            | RunError::Journal(JournalError::Store(StoreError::LeaseLost { .. })))
    )
}

/// Renew `lease` every third of `ttl`, each beat a third of `ttl` after the
/// one before it began; resolve only once a renewal finds it lost while the
/// run has not settled. A renewal that fails otherwise (the database is
/// unreachable) or outlasts its third is given up, and the next beat tries
/// again.
async fn beat(
    store: Arc<dyn RunStore>,
    lease: Lease,
    ttl: Duration,
    settled: Option<Arc<AtomicBool>>,
) -> StoreError {
    let every = ttl / 3;
    let mut due = Instant::now() + every;
    loop {
        tokio::time::sleep_until(due).await;
        due = Instant::now() + every;
        let renewed = tokio::time::timeout(every, store.renew(&lease)).await;
        if let Ok(Err(error @ StoreError::LeaseLost { .. })) = renewed {
            let settled = settled.as_ref().is_some_and(|f| f.load(Ordering::SeqCst));
            if !settled {
                return error;
            }
            // The run suspended or ended here; a later claim is the next
            // driver's, and this run is about to return on its own.
            return pending().await;
        }
    }
}
