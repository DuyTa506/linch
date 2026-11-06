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
//!
//! A background child also leaves its durable events, wrapped, for the
//! run's next turn, and its end as a [`Completion`] in the family's inbox.

use std::collections::{HashMap, HashSet};
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
use crate::events::Event;
use crate::ids::RunId;
use crate::subagents::Completion;

/// The child runs one run drives.
pub(crate) struct Family {
    run_id: RunId,
    /// The run's cancel token: background children stop with the run.
    cancel: CancellationToken,
    /// The agent whose [`Families`] lists this family once it is live.
    agent: Mutex<Weak<AgentInner>>,
    children: Mutex<FuturesUnordered<BoxFuture<'static, ()>>>,
    /// A child joined since the stream last polled the children.
    joined: AtomicBool,
    state: Mutex<State>,
    changed: Notify,
}

#[derive(Default)]
struct State {
    /// The children still running.
    live: HashMap<RunId, Live>,
    /// Background children that ended, not delivered yet.
    done: Vec<Completion>,
    /// Wrapped events of background children, for the run's log.
    log: Vec<Event>,
    /// Children `TaskStop` stopped, whose end needs no notification.
    stopped: HashSet<RunId>,
}

struct Live {
    cancel: CancellationToken,
    background: bool,
}

/// The guarded value even if a holder panicked; nothing panics while
/// holding these locks.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Family {
    /// The family of `run_id`, whose background children stop on `cancel`.
    pub(crate) fn new(run_id: RunId, cancel: CancellationToken) -> Arc<Self> {
        Arc::new(Self {
            run_id,
            cancel,
            agent: Mutex::new(Weak::new()),
            children: Mutex::new(FuturesUnordered::new()),
            joined: AtomicBool::new(false),
            state: Mutex::new(State::default()),
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

    /// A cancel token for a background child: cancelled with the run.
    pub(crate) fn child_token(&self) -> CancellationToken {
        self.cancel.child_token()
    }

    /// Drive `child` with `drive`, which ends once the child's stream does.
    /// `cancel` stops the child: the run cancels every child still running
    /// before it ends. A `background` child keeps the run's text answer
    /// from ending the run while it runs.
    pub(crate) fn adopt(
        self: &Arc<Self>,
        child: RunId,
        cancel: CancellationToken,
        background: bool,
        drive: impl Future<Output = ()> + Send + 'static,
    ) {
        let live = Live { cancel, background };
        lock(&self.state).live.insert(child.clone(), live);
        let family = Arc::downgrade(self);
        let task = async move {
            drive.await;
            if let Some(family) = family.upgrade() {
                lock(&family.state).live.remove(&child);
                family.changed.notify_waiters();
            }
        };
        lock(&self.children).push(Box::pin(task));
        self.joined.store(true, Ordering::SeqCst);
    }

    /// Keep a background child's wrapped event for the run's log.
    pub(crate) fn report(&self, event: Event) {
        lock(&self.state).log.push(event);
    }

    /// Put a background child's end in the inbox.
    pub(crate) fn complete(&self, mut completion: Completion) {
        let mut state = lock(&self.state);
        completion.stopped = state.stopped.remove(&completion.child);
        state.done.push(completion);
        drop(state);
        self.changed.notify_waiters();
    }

    /// Whether the child `child` runs.
    pub(crate) fn is_running(&self, child: &RunId) -> bool {
        lock(&self.state).live.contains_key(child)
    }

    /// Stop the running child `child`: cancel it, and record its end
    /// without a notification.
    pub(crate) fn stop(&self, child: &RunId) {
        let mut state = lock(&self.state);
        if let Some(live) = state.live.get(child) {
            live.cancel.cancel();
            state.stopped.insert(child.clone());
        }
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

    /// The wrapped events kept so far, in order.
    pub(super) fn take_log(&self) -> Vec<Event> {
        std::mem::take(&mut lock(&self.state).log)
    }

    /// The background ends not delivered yet, in order.
    pub(super) fn take_done(&self) -> Vec<Completion> {
        std::mem::take(&mut lock(&self.state).done)
    }

    /// Whether a background child runs, or one ended undelivered.
    pub(super) fn has_background(&self) -> bool {
        let state = lock(&self.state);
        !state.done.is_empty() || state.live.values().any(|live| live.background)
    }

    /// Wait until a background end is undelivered or no background child
    /// runs. The stream keeps polling the children meanwhile.
    pub(super) async fn settled(&self) {
        self.wait(|state| {
            !state.done.is_empty() || !state.live.values().any(|live| live.background)
        })
        .await;
    }

    /// Cancel every child still running.
    pub(super) fn cancel_all(&self) {
        for live in lock(&self.state).live.values() {
            live.cancel.cancel();
        }
    }

    /// Wait until no child runs.
    pub(super) async fn idle(&self) {
        self.wait(|state| state.live.is_empty()).await;
    }

    async fn wait(&self, ready: impl Fn(&State) -> bool) {
        loop {
            let mut changed = pin!(self.changed.notified());
            changed.as_mut().enable();
            if ready(&lock(&self.state)) {
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
