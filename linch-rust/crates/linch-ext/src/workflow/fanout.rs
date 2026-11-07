//! Fan-out: run branches concurrently, failing fast or collecting every
//! outcome.
//!
//! A branch is any future. Futures do nothing until polled, so there is no
//! thunk to wrap; every branch runs in the caller's task, nothing is
//! spawned. Each call gets its own limit of `max_concurrency` branches, so
//! a nested fan-out never waits on the slots its caller holds. Each branch
//! is polled under its branch path, so the journaled calls it makes count
//! apart from its siblings' (see `WorkflowContext::next_occurrence`).

use std::future::Future;

use futures::future::{join_all, try_join_all};
use tokio::sync::Semaphore;

use super::context::{WorkflowContext, in_branch};
use super::error::WorkflowError;

impl WorkflowContext {
    /// Run `branches` concurrently, at most
    /// [`max_concurrency`](super::WorkflowOptions::max_concurrency) at once,
    /// and return their values in input order.
    ///
    /// The first branch that fails drops the others, so a failed fan-out
    /// stops spending at once; a dropped branch journals nothing and runs
    /// again on resume. An interrupt in one branch drops the others too, so
    /// a fan-out cannot collect several answers at once: ask in sequence.
    ///
    /// Returns:
    ///     Every branch's value, in input order.
    ///
    /// Raises:
    ///     WorkflowError: The first error a branch returned.
    pub async fn parallel<T, Fut>(
        &self,
        branches: impl IntoIterator<Item = Fut>,
    ) -> Result<Vec<T>, WorkflowError>
    where
        Fut: Future<Output = Result<T, WorkflowError>>,
    {
        let slots = Semaphore::new(self.inner.max_concurrency);
        let branches = branches.into_iter().enumerate();
        try_join_all(branches.map(|(index, branch)| in_branch(index, gated(&slots, branch)))).await
    }

    /// Like [`parallel`](Self::parallel), but a failing branch leaves the
    /// others running, and every outcome is returned.
    ///
    /// A suspend is control flow, not an outcome: once every branch has
    /// finished, the first [`WorkflowError::Suspended`] is returned instead
    /// of the list.
    ///
    /// Returns:
    ///     One outcome per branch, in input order.
    ///
    /// Raises:
    ///     WorkflowError: `Suspended` when a branch hit an unanswered
    ///         interrupt.
    pub async fn settled<T, Fut>(
        &self,
        branches: impl IntoIterator<Item = Fut>,
    ) -> Result<Vec<Result<T, WorkflowError>>, WorkflowError>
    where
        Fut: Future<Output = Result<T, WorkflowError>>,
    {
        let slots = Semaphore::new(self.inner.max_concurrency);
        let branches = branches.into_iter().enumerate();
        let branches = branches.map(|(index, branch)| in_branch(index, gated(&slots, branch)));
        let mut outcomes = join_all(branches).await;
        let suspended = (outcomes.iter())
            .position(|outcome| matches!(outcome, Err(WorkflowError::Suspended(_))));
        match suspended.map(|index| outcomes.swap_remove(index)) {
            Some(Err(suspend)) => Err(suspend),
            _ => Ok(outcomes),
        }
    }
}

/// Run `branch` once it holds one of `slots`.
async fn gated<T>(slots: &Semaphore, branch: impl Future<Output = T>) -> T {
    // The semaphore is never closed, so the acquire cannot fail; the
    // permit is held until the branch ends.
    let _slot = slots.acquire().await;
    branch.await
}
