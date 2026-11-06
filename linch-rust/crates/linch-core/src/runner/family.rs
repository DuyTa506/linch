//! [`Family`]: the subagent child runs of one run, driven by the run's
//! [`RunStream`](super::RunStream).
//!
//! The core spawns no task, so a child run advances only while its
//! parent's stream is polled: the stream polls every child the run adopted
//! next to the run itself. A child outlives the call that started it: a
//! call that its timeout or the run's cancel cut short leaves its child to
//! finish its own abort, and the run waits for every child before its
//! terminal write. Dropping the stream drops the children with it, like a
//! crash; resuming the run picks them up again.

use std::collections::HashMap;
use std::future::Future;
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::task::{Context, Poll};

use futures::StreamExt;
use futures::future::BoxFuture;
use futures::stream::FuturesUnordered;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

use crate::agent::AgentInner;
use crate::ids::RunId;

/// The child runs one run drives.
pub(crate) struct Family {
    run_id: RunId,
    /// The agent whose [`Families`] lists this family once it is live.
    agent: Mutex<Weak<AgentInner>>,
    children: Mutex<FuturesUnordered<BoxFuture<'static, ()>>>,
    /// A child joined since the stream last polled the children.
    joined: AtomicBool,
    /// The children still running, with their cancel tokens.
    live: Mutex<HashMap<RunId, CancellationToken>>,
    changed: Notify,
}

/// The guarded value even if a holder panicked; nothing panics while
/// holding these locks.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Family {
    pub(crate) fn new(run_id: RunId) -> Arc<Self> {
        Arc::new(Self {
            run_id,
            agent: Mutex::new(Weak::new()),
            children: Mutex::new(FuturesUnordered::new()),
            joined: AtomicBool::new(false),
            live: Mutex::new(HashMap::new()),
            changed: Notify::new(),
        })
    }

    /// List this family under its run on `agent`, once the run is created
    /// or claimed, so the run's tools find it. A later family of the same
    /// run (a resume that fenced this one) takes the entry over.
    pub(crate) fn register(self: &Arc<Self>, agent: &Arc<AgentInner>) {
        *lock(&self.agent) = Arc::downgrade(agent);
        let mut families = lock(&agent.families);
        families.insert(self.run_id.clone(), Arc::downgrade(self));
    }

    /// Drive `child` with `drive`, which ends once the child's stream does.
    /// `cancel` stops the child: the run cancels every child still running
    /// before it ends.
    pub(crate) fn adopt(
        self: &Arc<Self>,
        child: RunId,
        cancel: CancellationToken,
        drive: impl Future<Output = ()> + Send + 'static,
    ) {
        lock(&self.live).insert(child.clone(), cancel);
        let family = Arc::downgrade(self);
        let task = async move {
            drive.await;
            if let Some(family) = family.upgrade() {
                lock(&family.live).remove(&child);
                family.changed.notify_waiters();
            }
        };
        lock(&self.children).push(Box::pin(task));
        self.joined.store(true, Ordering::SeqCst);
    }

    /// Poll every child once it can go on.
    pub(super) fn poll(&self, cx: &mut Context<'_>) {
        self.joined.store(false, Ordering::SeqCst);
        let mut children = lock(&self.children);
        while let Poll::Ready(Some(())) = children.poll_next_unpin(cx) {}
    }

    /// Whether a child joined since the last [`Self::poll`], so it still
    /// needs its first poll.
    pub(super) fn joined(&self) -> bool {
        self.joined.load(Ordering::SeqCst)
    }

    /// Cancel every child still running.
    pub(super) fn cancel_all(&self) {
        for cancel in lock(&self.live).values() {
            cancel.cancel();
        }
    }

    /// Wait until no child runs. The stream keeps polling the children
    /// meanwhile.
    pub(super) async fn idle(&self) {
        loop {
            let mut changed = pin!(self.changed.notified());
            changed.as_mut().enable();
            if lock(&self.live).is_empty() {
                return;
            }
            changed.await;
        }
    }
}

impl Drop for Family {
    fn drop(&mut self) {
        let Some(agent) = lock(&self.agent).upgrade() else {
            return;
        };
        let mut families = lock(&agent.families);
        let ours =
            (families.get(&self.run_id)).is_some_and(|entry| std::ptr::eq(entry.as_ptr(), self));
        if ours {
            families.remove(&self.run_id);
        }
    }
}

/// The live families of an agent's runs, by run id.
pub(crate) type Families = Mutex<HashMap<RunId, Weak<Family>>>;

impl AgentInner {
    /// The family of `run_id`, while its stream lives.
    pub(crate) fn family(&self, run_id: &RunId) -> Option<Arc<Family>> {
        lock(&self.families).get(run_id).and_then(Weak::upgrade)
    }
}
