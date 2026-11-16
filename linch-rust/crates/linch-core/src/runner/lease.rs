//! Keeping a claimed run's lease alive while the run is driven.
//!
//! A store whose claims expire ([`RunStore::lease_ttl`]) refuses a claim
//! from another holder while the lease is alive. [`Held`] renews it every
//! third of the TTL, on a timer, so a long model call, a tool or a retry
//! back-off never lets it lapse. The heartbeat is part of the run future:
//! it beats only while the caller polls the stream, so a stream nobody
//! polls (a stalled or crashed host) lets its lease run out and another
//! process takes the run over.
//!
//! A renewal that finds the epoch moved (another holder seized the run)
//! ends the run at once with `LeaseLost`, as a fenced write would, unless
//! the run already suspended or ended. A run that stops without suspending
//! or ending (an error, a dropped stream) releases its lease, so another
//! process need not wait out the TTL. With a store that has no TTL (the
//! memory stores) nothing of this runs.

use std::future::{Future, pending};
use std::pin::pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use futures::future::{Either, select};

use crate::agent::RunError;
use crate::journal::{Journal, JournalError};
use crate::store::{Lease, RunStore, StoreError};

/// The lease of one claimed run: renewed while the run is driven, released
/// when the run stops without settling.
pub(super) struct Held {
    store: Arc<dyn RunStore>,
    lease: Lease,
    /// The store's TTL; `None` when claims never expire.
    ttl: Option<Duration>,
    /// The journal's flag: the run suspended or ended.
    settled: Option<Arc<AtomicBool>>,
    /// The run future returned `Ok`, or the lease is gone: nothing to release.
    done: bool,
}

impl Held {
    /// The lease just claimed on `store`.
    pub(super) fn new(store: &Arc<dyn RunStore>, lease: &Lease) -> Self {
        Self {
            store: Arc::clone(store),
            lease: lease.clone(),
            ttl: store.lease_ttl(),
            settled: None,
            done: false,
        }
    }

    /// Watch `journal`, which writes under this lease, for the run settling.
    pub(super) fn watch(&mut self, journal: &Journal) {
        self.settled = Some(journal.settled());
    }

    /// Drive `run` with the lease renewed next to it.
    ///
    /// Returns:
    ///     What `run` returned, or a `LeaseLost` journal error once a
    ///     renewal found the run claimed by another holder.
    pub(super) async fn drive(
        &mut self,
        run: impl Future<Output = Result<(), RunError>>,
    ) -> Result<(), RunError> {
        let Some(ttl) = self.ttl else {
            return run.await;
        };
        let beat = beat(&*self.store, &self.lease, ttl, self.settled.clone());
        let result = match select(pin!(run), pin!(beat)).await {
            Either::Left((result, _)) => result,
            Either::Right((lost, _)) => Err(RunError::Journal(JournalError::Store(lost))),
        };
        // A lost lease is not ours to release.
        self.done = result.is_ok() || lost(&result);
        result
    }
}

impl Drop for Held {
    fn drop(&mut self) {
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
        runtime.spawn(async move {
            let _ = store.release(&lease).await;
        });
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

/// Renew `lease` every third of `ttl`; resolve only once a renewal finds
/// it lost while the run has not settled. A renewal that fails otherwise
/// (the database is unreachable) is tried again at the next beat.
async fn beat(
    store: &dyn RunStore,
    lease: &Lease,
    ttl: Duration,
    settled: Option<Arc<AtomicBool>>,
) -> StoreError {
    let every = ttl / 3;
    loop {
        tokio::time::sleep(every).await;
        if let Err(error @ StoreError::LeaseLost { .. }) = store.renew(lease).await {
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
