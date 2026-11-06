//! The run state machine (card P4-D) and the caller-driven [`RunStream`].
//!
//! One turn:
//! 1. when compaction is on and the provider view passed its threshold,
//!    summarize the older part and log the projection (see `compact`);
//!    snapshot the tools and build the request from the provider view, with
//!    the `context_build` blocks added to its tail, never logged;
//! 2. stage a `context_build` event (the request's system block, message
//!    and tool counts) and the model-call delivery, and flush them with the
//!    held `ProviderPending` checkpoint, then drive the call (deltas go to
//!    the caller at once);
//! 3. stage the receipt, the `Assistant` event and its `Usage`, and write
//!    them through with `AssistantReceived`. A response the output limit
//!    cut off before it held any text or whole tool call is not an answer:
//!    its receipt (rejected) and `Usage` go out with an `OutputLimitError`
//!    in the failed terminal write, and the session gets nothing;
//! 4. append the response to the session (the held checkpoint is flushed
//!    first); a text-only response ends the run unless a `stop` hook keeps
//!    it going with feedback for the next turn;
//! 5. plan the tool calls. A call awaiting approval suspends the run with a
//!    write-through `AwaitingApproval`. Otherwise the plan's decisions are
//!    held in `ToolBatch` and the batch runs: every tool that really runs
//!    flushes its start before it runs and its end after;
//! 6. hold `ToolResults`, append the results to the session, run
//!    `post_turn`, next turn.
//!
//! A new run's prompt passes `user_prompt_submit` before anything is
//! written; a blocked prompt creates no run. Hooks are never barriers.
//!
//! A session holds at most one run that is not terminal: the store refuses
//! the create of a second one ([`crate::store::StoreError::SessionBusy`])
//! before anything of it is written, so two runs never interleave in one
//! session log. A run that ended between a new run's session load and its
//! create is read again before the new run's prompt goes in.
//!
//! A tool turn with `k` started tools costs `3 + 2k` run writes and 2 session
//! writes. The core spawns no task: [`RunStream`] holds the run future and
//! polls it only while the caller polls the stream.

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll};

use futures::Stream;
use futures::future::BoxFuture;

use crate::agent::{AgentInner, RunError, RunOptions};

use crate::events::Envelope;
use crate::hooks::ApprovalResponse;
use crate::ids::RunId;
pub(crate) use fail::fail;
pub(crate) use family::{Families, Family};

mod batch;
mod compact;
mod end;
mod fail;
mod family;
mod hooks;
mod inbox;
mod progress;
mod sink;
mod start;
mod turn;
mod write;

/// The envelopes of one run, in order, as the caller polls for them.
///
/// Durable kinds arrive only after the write that made them durable; deltas
/// and tool progress arrive at once and carry the last durable seq. The
/// stream ends after the `Result` or `Suspended` envelope. When the run
/// cannot continue durably, the last item is `Err(RunError)`. A resume or
/// approve whose cancel token is already cancelled ends with no item at
/// all: it leaves the run as it was.
///
/// Dropping the stream drops the run where it stands, like a crash: the run
/// keeps its last durable state and [`crate::agent::Agent::resume`] continues
/// it.
pub struct RunStream {
    run_id: RunId,
    outbox: Outbox,
    run: Option<BoxFuture<'static, Result<(), RunError>>>,
    /// The subagent child runs this stream drives next to the run; `None`
    /// for an agent without subagents.
    family: Option<Arc<Family>>,
    error: Option<RunError>,
}

impl RunStream {
    fn new(
        run_id: RunId,
        outbox: Outbox,
        run: BoxFuture<'static, Result<(), RunError>>,
        family: Option<Arc<Family>>,
    ) -> Self {
        Self {
            run_id,
            outbox,
            run: Some(run),
            family,
            error: None,
        }
    }

    /// The run this stream drives.
    pub fn run_id(&self) -> &RunId {
        &self.run_id
    }
}

impl Stream for RunStream {
    type Item = Result<Envelope, RunError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        loop {
            if let Some(envelope) = this.outbox.pop() {
                return Poll::Ready(Some(Ok(envelope)));
            }
            if let Some(family) = &this.family {
                family.poll(cx);
            }
            let Some(run) = this.run.as_mut() else {
                return Poll::Ready(this.error.take().map(Err));
            };
            match run.as_mut().poll(cx) {
                Poll::Ready(result) => {
                    this.run = None;
                    this.error = result.err();
                }
                // A child joined while the run was polled: poll it once.
                Poll::Pending if this.family.as_ref().is_some_and(|f| f.joined()) => {}
                // The run parked on real I/O; hand over what it queued.
                Poll::Pending if this.outbox.is_empty() => return Poll::Pending,
                Poll::Pending => {}
            }
        }
    }
}

/// Start a new run (see [`crate::agent::Agent::run`]).
pub(crate) fn start(agent: Arc<AgentInner>, prompt: String, options: RunOptions) -> RunStream {
    start_under(agent, prompt, options, None)
}

/// Start a subagent's child run of `parent` (see [`crate::subagents`]).
pub(crate) fn start_child(
    agent: Arc<AgentInner>,
    prompt: String,
    options: RunOptions,
    parent: RunId,
) -> RunStream {
    start_under(agent, prompt, options, Some(parent))
}

fn start_under(
    agent: Arc<AgentInner>,
    prompt: String,
    options: RunOptions,
    parent: Option<RunId>,
) -> RunStream {
    let run_id =
        (options.run_id.clone()).unwrap_or_else(|| RunId::new(uuid::Uuid::new_v4().to_string()));
    let outbox = Outbox::default();
    let family = family(&agent, &run_id);
    let run = start::new_run(
        agent,
        run_id.clone(),
        prompt,
        options,
        Shared {
            outbox: outbox.clone(),
            family: family.clone(),
        },
        parent,
    );
    RunStream::new(run_id, outbox, Box::pin(run), family)
}

/// A family for the child runs of `run_id`, when the agent has subagents.
fn family(agent: &AgentInner, run_id: &RunId) -> Option<Arc<Family>> {
    agent
        .subagents
        .as_ref()
        .map(|_| Family::new(run_id.clone()))
}

/// Resume a run, or approve its pending calls when `answers` is set.
pub(crate) fn resume(
    agent: Arc<AgentInner>,
    run_id: RunId,
    answers: Option<HashMap<String, ApprovalResponse>>,
    options: RunOptions,
) -> RunStream {
    let outbox = Outbox::default();
    let family = family(&agent, &run_id);
    let shared = Shared {
        outbox: outbox.clone(),
        family: family.clone(),
    };
    let run = start::continue_run(agent, run_id.clone(), answers, options, shared);
    RunStream::new(run_id, outbox, Box::pin(run), family)
}

/// What a run future shares with its stream.
pub(super) struct Shared {
    pub(super) outbox: Outbox,
    pub(super) family: Option<Arc<Family>>,
}

/// Envelopes waiting for the caller, shared by the run future and its
/// stream.
#[derive(Clone, Default)]
pub(crate) struct Outbox(Arc<Mutex<VecDeque<Envelope>>>);

impl Outbox {
    /// Queue one envelope without yielding (deltas and progress, from sync
    /// callbacks).
    fn push(&self, envelope: Envelope) {
        self.lock().push_back(envelope);
    }

    /// Queue `envelopes` and yield once, so the stream hands them over
    /// before the run goes on.
    async fn deliver(&self, envelopes: Vec<Envelope>) {
        if envelopes.is_empty() {
            return;
        }
        self.lock().extend(envelopes);
        YieldOnce(false).await;
    }

    fn pop(&self) -> Option<Envelope> {
        self.lock().pop_front()
    }

    fn is_empty(&self) -> bool {
        self.lock().is_empty()
    }

    fn lock(&self) -> MutexGuard<'_, VecDeque<Envelope>> {
        // Nothing panics while holding the lock.
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Pending once, then ready: returns control to [`RunStream::poll_next`].
struct YieldOnce(bool);

impl Future for YieldOnce {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.0 {
            return Poll::Ready(());
        }
        self.0 = true;
        cx.waker().wake_by_ref();
        Poll::Pending
    }
}
