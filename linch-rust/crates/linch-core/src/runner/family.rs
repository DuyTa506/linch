//! [`Family`]: the subagent child runs of one run. The agent lists it by
//! run id from the run's create or claim until the run's stream lets go of
//! it, so it outlives a stream that ends with the run suspended.
//!
//! While a stream drives the run, the core spawns no task: the stream
//! polls every child the run adopted next to the run itself. A child
//! outlives the call that started it: a call that its timeout or the run's
//! cancel cut short leaves its child to finish its own abort, and the run
//! waits for every child before its terminal write.
//!
//! A run that suspends for approval keeps its children running: its stream
//! hands them to one task, spawned on the caller's Tokio runtime, which
//! drives them until a stream of the run attaches again (an approve or a
//! resume in this process) or none is left. Without a runtime they wait,
//! unpolled, for that stream. Closing the run's session or dropping the
//! agent cancels them, and the task drives them until their aborts are
//! durable. A stream that stops any other way (dropped, or its run failed)
//! drops the children with the family, like a crash; resuming the run picks
//! them up again from their durable state.
//!
//! A background child also leaves its durable events, wrapped, for the
//! run's next turn, and its end as a [`Completion`] in the family's inbox.

use std::collections::{HashMap, HashSet};
use std::future::{Future, poll_fn};
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::task::{Context, Poll, Waker};

use futures::StreamExt;
use futures::future::BoxFuture;
use futures::stream::FuturesUnordered;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

use crate::agent::AgentInner;
use crate::events::Event;
use crate::ids::{RunId, SessionId};
use crate::subagents::Completion;

/// The child runs one run drives.
pub(crate) struct Family {
    run_id: RunId,
    session_id: SessionId,
    /// Cancelled when the family closes; background children take a child
    /// token of it.
    cancel: CancellationToken,
    /// The agent whose [`Families`] list this family.
    agent: Weak<AgentInner>,
    drive: Mutex<Drive>,
    /// A child joined, or a stream attached, since the children were last
    /// polled.
    joined: AtomicBool,
    state: Mutex<State>,
    changed: Notify,
}

/// The children and who polls them.
#[derive(Default)]
struct Drive {
    children: FuturesUnordered<BoxFuture<'static, ()>>,
    /// The id of the stream that polls the children now; each stream that
    /// attaches takes the next one, and the task of a suspended run polls
    /// under its stream's id until another one attaches.
    driver: u64,
    /// The run is suspended: its children outlive its stream.
    parked: bool,
    /// The session closed or the agent went: the children were cancelled,
    /// and the family leaves the agent once none is left.
    closed: bool,
    /// The task driving a suspended run's children, woken when a stream
    /// takes them over.
    keeper: Option<Waker>,
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
    /// A cancel token for a background child: cancelled when the family
    /// closes.
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
        lock(&self.drive).children.push(Box::pin(task));
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

    /// Whether the child `child` runs, or ended and is not delivered yet.
    pub(crate) fn knows(&self, child: &RunId) -> bool {
        let state = lock(&self.state);
        state.live.contains_key(child) || state.done.iter().any(|done| done.child == *child)
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

    /// Take the children over for a stream of the run; returns the id it
    /// polls them with.
    pub(super) fn attach(&self) -> u64 {
        let mut drive = lock(&self.drive);
        drive.driver += 1;
        drive.parked = false;
        let keeper = drive.keeper.take();
        let driver = drive.driver;
        drop(drive);
        self.joined.store(true, Ordering::SeqCst);
        if let Some(keeper) = keeper {
            keeper.wake();
        }
        driver
    }

    /// Poll every child that can go on, if `driver` still drives them.
    /// `None` when another stream took them over; else whether none is
    /// left. The task of a suspended run (`keeper`) leaves its waker for
    /// the stream that takes over.
    fn poll(&self, cx: &mut Context<'_>, driver: u64, keeper: bool) -> Option<bool> {
        let mut drive = lock(&self.drive);
        if drive.driver != driver {
            return None;
        }
        self.joined.store(false, Ordering::SeqCst);
        while let Poll::Ready(Some(())) = drive.children.poll_next_unpin(cx) {}
        if keeper {
            drive.keeper = Some(cx.waker().clone());
        }
        Some(drive.children.is_empty())
    }

    /// Poll the children for the stream `driver`.
    pub(super) fn poll_for(&self, cx: &mut Context<'_>, driver: u64) {
        self.poll(cx, driver, false);
    }

    /// Whether a child joined, or a stream attached, since the last poll,
    /// so the children still need a poll.
    pub(super) fn joined(&self) -> bool {
        self.joined.load(Ordering::SeqCst)
    }

    /// Mark the run suspended (or not, when its suspend failed): its
    /// children then outlive its stream.
    pub(super) fn park(&self, parked: bool) {
        lock(&self.drive).parked = parked;
    }

    /// The stream `driver` stopped. A suspended run's children go on in a
    /// task of their own; any other run's go with the family, like a crash.
    pub(super) fn detach(self: &Arc<Self>, driver: u64) {
        let mut drive = lock(&self.drive);
        if drive.driver != driver {
            return;
        }
        if drive.parked {
            let empty = drive.children.is_empty();
            drop(drive);
            if !empty {
                self.keep(driver);
            }
            return;
        }
        let children = std::mem::take(&mut drive.children);
        drop(drive);
        self.release();
        drop(children);
    }

    /// Drive a suspended run's children until a stream takes them over or
    /// none is left, on the caller's Tokio runtime.
    fn keep(self: &Arc<Self>, driver: u64) {
        let Ok(runtime) = tokio::runtime::Handle::try_current() else {
            return;
        };
        let family = Arc::clone(self);
        runtime.spawn(async move {
            let drained = poll_fn(|cx| match family.poll(cx, driver, true) {
                Some(false) => Poll::Pending,
                left => Poll::Ready(left.is_some()),
            })
            .await;
            if drained && lock(&family.drive).closed {
                family.release();
            }
        });
    }

    /// Close a suspended run's family (its session closed): cancel its
    /// children, which write their aborts. It leaves the agent once none
    /// is left; a later approve or resume hears their ends.
    pub(crate) fn close_parked(&self) {
        if lock(&self.drive).parked {
            self.close();
        }
    }

    /// Cancel every child and leave the agent once none is left.
    pub(crate) fn close(&self) {
        let mut drive = lock(&self.drive);
        drive.closed = true;
        let empty = drive.children.is_empty();
        drop(drive);
        self.cancel.cancel();
        self.cancel_all();
        if empty {
            self.release();
        }
    }

    /// Leave the agent's list, unless another family took the entry.
    fn release(&self) {
        let Some(agent) = self.agent.upgrade() else {
            return;
        };
        let mut families = lock(&agent.families);
        let ours = (families.get(&self.run_id)).is_some_and(|entry| std::ptr::eq(&**entry, self));
        if ours {
            families.remove(&self.run_id);
        }
    }

    /// The wrapped events kept so far, in order.
    pub(super) fn take_log(&self) -> Vec<Event> {
        std::mem::take(&mut lock(&self.state).log)
    }

    /// The background ends not delivered yet, in order.
    pub(super) fn take_done(&self) -> Vec<Completion> {
        std::mem::take(&mut lock(&self.state).done)
    }

    /// How many background children run.
    pub(crate) fn background_count(&self) -> usize {
        let state = lock(&self.state);
        state.live.values().filter(|live| live.background).count()
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

    /// While the run is suspended with children running, a future that
    /// resolves once none runs; `None` otherwise.
    pub(crate) fn parked_children(
        self: &Arc<Self>,
    ) -> Option<impl Future<Output = ()> + Send + use<>> {
        let parked = lock(&self.drive).parked;
        let running = !lock(&self.state).live.is_empty();
        let family = Arc::clone(self);
        (parked && running).then_some(async move { family.idle().await })
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

/// The families of an agent's runs, by run id.
pub(crate) type Families = Mutex<HashMap<RunId, Arc<Family>>>;

impl AgentInner {
    /// The family of `run_id`, while the agent lists it.
    pub(crate) fn family(&self, run_id: &RunId) -> Option<Arc<Family>> {
        lock(&self.families).get(run_id).cloned()
    }

    /// The listed family of `run_id` in `session_id`, or a new one listed
    /// now.
    pub(crate) fn family_for(
        self: &Arc<Self>,
        run_id: &RunId,
        session_id: &SessionId,
    ) -> Arc<Family> {
        let mut families = lock(&self.families);
        let family = families.entry(run_id.clone()).or_insert_with(|| {
            Arc::new(Family {
                run_id: run_id.clone(),
                session_id: session_id.clone(),
                cancel: CancellationToken::new(),
                agent: Arc::downgrade(self),
                drive: Mutex::default(),
                joined: AtomicBool::new(false),
                state: Mutex::default(),
                changed: Notify::new(),
            })
        });
        Arc::clone(family)
    }

    /// Close the family of each suspended run of `session_id` (see
    /// [`Family::close_parked`]).
    pub(crate) fn close_families(&self, session_id: &SessionId) {
        let families: Vec<Arc<Family>> = lock(&self.families).values().cloned().collect();
        for family in families {
            if family.session_id == *session_id {
                family.close_parked();
            }
        }
    }

    /// Close every family; the agent is going.
    pub(crate) fn close_all_families(&self) {
        let families = std::mem::take(&mut *lock(&self.families));
        for family in families.into_values() {
            family.close();
        }
    }
}
