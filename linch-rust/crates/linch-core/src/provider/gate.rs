//! A shared cap on concurrent provider calls.
//!
//! A [`ProviderGate`] is the proactive counterpart of the retry policy
//! (see [`crate::reliability`]): that reacts after a provider says no, this
//! holds calls back so it has less reason to. Python's `max_provider_concurrency`
//! is the same thing, a semaphore around every live provider call. There is
//! no rate or token limit, as there is none in Python's.
//!
//! Every gate clone shares one budget. An agent holds one for all its runs
//! and its subagents' (see [`AgentBuilder::provider_gate`]); the facade's
//! `PgPools` hands the same gate to every agent opened with the same key and
//! endpoint, so a server's tenant stays under its cap however many agents and
//! runs it opens. A gate never leaves its process: runs in another process
//! (a worker, a second server) have their own budget.
//!
//! Waiters are served in the order they ask (first in, first out), as with
//! Python's `asyncio.Semaphore`.
//!
//! [`AgentBuilder::provider_gate`]: crate::AgentBuilder::provider_gate

use std::num::NonZeroU32;
use std::sync::{Arc, Weak};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio_util::sync::CancellationToken;

#[derive(Debug)]
struct Shared {
    semaphore: Arc<Semaphore>,
    max: NonZeroU32,
}

/// At most `max` provider calls at once, across every clone.
///
/// Cloning shares the budget; it does not add to it.
#[derive(Debug, Clone)]
pub struct ProviderGate(Arc<Shared>);

/// A [`ProviderGate`] that does not keep it alive, for a registry that must
/// not hold a gate no agent uses any more.
#[derive(Debug, Clone)]
pub struct WeakProviderGate(Weak<Shared>);

/// One provider call's place in its gate, given back when it drops.
#[derive(Debug)]
pub struct ProviderPermit {
    _permit: OwnedSemaphorePermit,
    _gate: ProviderGate,
}

impl ProviderGate {
    /// A gate that lets `max` calls run at once.
    pub fn new(max: NonZeroU32) -> Self {
        let semaphore = Arc::new(Semaphore::new(max.get() as usize));
        Self(Arc::new(Shared { semaphore, max }))
    }

    /// The most calls this gate lets run at once.
    pub fn max(&self) -> NonZeroU32 {
        self.0.max
    }

    /// How many calls hold a place now.
    pub fn in_flight(&self) -> usize {
        (self.0.max.get() as usize).saturating_sub(self.0.semaphore.available_permits())
    }

    /// A handle that does not keep the gate alive.
    pub fn downgrade(&self) -> WeakProviderGate {
        WeakProviderGate(Arc::downgrade(&self.0))
    }

    /// A place now, or `None` when the gate is full.
    pub fn try_acquire(&self) -> Option<ProviderPermit> {
        let permit = Arc::clone(&self.0.semaphore).try_acquire_owned().ok()?;
        Some(self.permit(permit))
    }

    /// Wait for a place, first come first served.
    ///
    /// Args:
    ///     cancel: Gives up the wait. A waiter that gives up leaves the
    ///         queue and takes no place.
    ///
    /// Returns:
    ///     The place, or `None` when `cancel` ended the wait first.
    pub async fn acquire(&self, cancel: &CancellationToken) -> Option<ProviderPermit> {
        let waiting = Arc::clone(&self.0.semaphore).acquire_owned();
        let permit = cancel.run_until_cancelled(waiting).await?;
        // The semaphore is never closed.
        Some(self.permit(permit.ok()?))
    }

    fn permit(&self, permit: OwnedSemaphorePermit) -> ProviderPermit {
        ProviderPermit {
            _permit: permit,
            _gate: self.clone(),
        }
    }
}

impl WeakProviderGate {
    /// The gate, unless every clone of it is gone.
    pub fn upgrade(&self) -> Option<ProviderGate> {
        self.0.upgrade().map(ProviderGate)
    }
}

#[cfg(test)]
mod tests;
