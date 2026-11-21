//! A run's wait for its agent's provider gate (see
//! [`crate::provider::ProviderGate`]).
//!
//! The place is taken before the call's delivery is staged: a run that
//! waits has dispatched nothing, so cancelling it leaves no delivery and no
//! receipt. It is held through the delivery's write and the call, and given
//! back before the response is handled, so a retry's backoff, a
//! compaction's summary call and the hooks never hold it.

use super::turn::Runner;
use crate::agent::RunError;
use crate::provider::ProviderPermit;

/// What waiting for the gate came to.
pub(super) enum Slot {
    /// The agent has no gate.
    Ungated,
    /// The call's place, held until it drops.
    Held(#[allow(dead_code)] ProviderPermit),
    /// The run was cancelled while it waited.
    Cancelled,
}

impl Runner {
    /// Take a place in the agent's gate for the call about to be staged.
    ///
    /// With a place free this writes nothing, so a run that never waits is
    /// what it was without a gate. Otherwise the events staged so far (a
    /// failed receipt and its usage, a fallback, a child's events) are made
    /// durable first: they must not wait in memory for a place that may
    /// take long, with a checkpoint at the totals that count any receipt
    /// among them.
    pub(super) async fn slot(&mut self) -> Result<Slot, RunError> {
        let Some(gate) = self.agent.gate.clone() else {
            return Ok(Slot::Ungated);
        };
        if let Some(permit) = gate.try_acquire() {
            return Ok(Slot::Held(permit));
        }
        self.flush_staged().await?;
        Ok(match gate.acquire(&self.cancel).await {
            Some(permit) => Slot::Held(permit),
            None => Slot::Cancelled,
        })
    }
}
