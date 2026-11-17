//! [`run_workflow`] and its [`WorkflowOptions`], and the
//! [`WorkflowSession`] a host that drives the calls itself (as the RPC
//! server does, for a client-side workflow function) opens and finishes.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use linch_core::JsonValue;
use tokio_util::sync::CancellationToken;

use super::context::{EventSink, LiveGate, WorkflowContext};
use super::error::WorkflowError;
use super::journal::{JournalLease, JournalStore, JournalStoreError, WorkflowHold, WorkflowStatus};

/// A spending cap shared by every agent call of one workflow.
///
/// It is checked before each agent call that would run live, against what
/// the workflow's finished calls spent, replayed ones included, so a resume
/// never starts over from zero. One running call can overshoot the cap; set
/// the agent's own `spec.budget` to bound a single call.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct WorkflowBudget {
    /// Most tokens the workflow's agent calls may spend, all four usage
    /// buckets summed.
    pub max_tokens: Option<u64>,
    /// Most USD they may spend, by the agent's pricing.
    pub max_cost_usd: Option<f64>,
}

/// How one [`run_workflow`] call journals and runs.
///
/// Timeouts, the deadline and retry backoff use `tokio::time`, so a
/// workflow that sets any of them must run on a Tokio runtime with the time
/// driver enabled.
#[derive(Clone)]
pub struct WorkflowOptions {
    /// Where the journal lives. Pass a durable store, and the same
    /// `namespace` and `workflow_id`, to resume across processes.
    pub journal: Arc<dyn JournalStore>,
    /// The store key space, for example the agent's namespace.
    pub namespace: String,
    /// Stable id of this workflow; calling again with it resumes.
    pub workflow_id: String,
    /// Answers to pending interrupts, by interrupt key. Each answer is
    /// used once, then journaled. An answer applies only to the question
    /// the workflow was parked on (its stored interrupt: same key, same
    /// payload); asked again with another payload, the interrupt suspends
    /// again and the answer is dropped with an `interrupt_stale` event.
    pub resume: HashMap<String, JsonValue>,
    /// Most branches one `parallel` or `settled` call runs at once; 0
    /// counts as 1. Each call, nested ones included, gets its own limit.
    pub max_concurrency: usize,
    /// Default limit on each step attempt; a step's own
    /// [`StepOptions::timeout`](super::StepOptions::timeout) wins.
    pub step_timeout: Option<Duration>,
    /// Limit on the whole workflow function. When it runs out the function
    /// is dropped, the run fails with [`WorkflowError::Timeout`], and the
    /// finished steps stay journaled for a resume.
    pub deadline: Option<Duration>,
    /// Stops the workflow at its next journaled call that would run live
    /// work; replays still return.
    pub cancel: CancellationToken,
    /// Receives every workflow event; `None` drops them.
    pub on_event: Option<EventSink>,
    /// Caps what the workflow's agent calls spend; `None` is no cap.
    pub budget: Option<WorkflowBudget>,
    /// Most records the journal may hold for this workflow, replayed ones
    /// included; a call that would add one past it fails with
    /// [`WorkflowError::LimitExceeded`]. `None` is no cap.
    pub max_records: Option<u64>,
    /// Most bytes one record's JSON value may take; a call whose value is
    /// larger fails with [`WorkflowError::LimitExceeded`] and journals
    /// nothing. `None` is no cap.
    pub max_record_bytes: Option<usize>,
    /// Asked before each agent call that would run live (never for a
    /// replay); its error fails the call, and the guard it returns is held
    /// until the call ends. A host that meters live work (places, a cap on
    /// calls in flight) goes here.
    pub live_gate: Option<LiveGate>,
}

impl WorkflowOptions {
    /// Options for workflow `workflow_id` of `namespace`, journaled in
    /// `journal`, with no answers, 4 branches at once, no timeouts, no
    /// caps, a fresh cancel token and no event sink.
    pub fn new(
        journal: Arc<dyn JournalStore>,
        namespace: impl Into<String>,
        workflow_id: impl Into<String>,
    ) -> Self {
        Self {
            journal,
            namespace: namespace.into(),
            workflow_id: workflow_id.into(),
            resume: HashMap::new(),
            max_concurrency: 4,
            step_timeout: None,
            deadline: None,
            cancel: CancellationToken::new(),
            on_event: None,
            budget: None,
            max_records: None,
            max_record_bytes: None,
            live_gate: None,
        }
    }
}

/// A claimed workflow whose calls the host drives itself.
///
/// [`run_workflow`] is built on it: [`Self::open`] claims the workflow and
/// seeds a [`WorkflowContext`] with the stored records, and
/// [`Self::finish`] writes the final status. A host whose workflow
/// function does not run in this process (the RPC server's client does)
/// holds the session across its calls instead.
pub struct WorkflowSession {
    journal: Arc<dyn JournalStore>,
    lease: JournalLease,
    context: WorkflowContext,
    stored: usize,
    previous: Option<WorkflowStatus>,
    /// Kept for as long as the session lives.
    _hold: WorkflowHold,
}

impl WorkflowSession {
    /// Claim the workflow (creating it when new), which fences out any
    /// process still running it, and load its records.
    ///
    /// Raises:
    ///     WorkflowError: `Store` when the claim or the load fails.
    pub async fn open(options: &WorkflowOptions) -> Result<Self, WorkflowError> {
        Self::open_with(options, WorkflowHold::default()).await
    }

    /// Like [`Self::open`], for a host that wants a second driver refused
    /// instead of fencing out the first: the store's
    /// [`hold`](JournalStore::hold) is taken before anything is written,
    /// so a refusal leaves the epoch and the status as they were, and is
    /// kept until the session drops.
    ///
    /// Raises:
    ///     WorkflowError: `Store` with [`JournalStoreError::Held`] while
    ///         another holder drives the workflow, or when the claim or the
    ///         load fails.
    pub async fn open_held(options: &WorkflowOptions) -> Result<Self, WorkflowError> {
        let hold = options
            .journal
            .hold(&options.namespace, &options.workflow_id)
            .await?;
        Self::open_with(options, hold).await
    }

    async fn open_with(
        options: &WorkflowOptions,
        hold: WorkflowHold,
    ) -> Result<Self, WorkflowError> {
        let journal = options.journal.clone();
        let (namespace, id) = (&options.namespace, &options.workflow_id);
        // Read before the claim, which sets the status to running.
        let before = journal.load(namespace, id).await?;
        let lease = journal.claim(namespace, id).await?;
        let stored = journal.load(namespace, id).await?;
        let previous = before.map(|run| run.status);
        let awaiting = match &previous {
            Some(WorkflowStatus::AwaitingInput(interrupt)) => Some(interrupt.clone()),
            _ => None,
        };
        let records = stored.map(|run| run.records).unwrap_or_default();
        let count = records.len();
        let context = WorkflowContext::new(lease.clone(), records, awaiting, options);
        Ok(Self {
            journal,
            lease,
            context,
            stored: count,
            previous,
            _hold: hold,
        })
    }

    /// Free the workflow's hold and wait until another driver can take it.
    /// Dropping the session frees it too, but maybe a moment later.
    pub async fn release(self) {
        self._hold.release().await;
    }

    /// The context the calls run on.
    pub fn context(&self) -> &WorkflowContext {
        &self.context
    }

    /// How many records the journal held when the workflow was claimed.
    pub fn stored(&self) -> usize {
        self.stored
    }

    /// The status the workflow had before this claim; `None` for a new one.
    pub fn previous(&self) -> Option<&WorkflowStatus> {
        self.previous.as_ref()
    }

    /// The lease epoch this claim holds.
    pub fn epoch(&self) -> u64 {
        self.lease.epoch
    }

    /// Write the final status: `awaiting_input` with the interrupt when one
    /// suspended the run, else `completed` when `ok` and `failed` when not.
    /// A lost lease writes nothing, since another process owns the
    /// workflow now.
    ///
    /// Raises:
    ///     WorkflowError: `Store` when the write fails, including a lost
    ///         lease.
    pub async fn finish(&self, ok: bool) -> Result<(), WorkflowError> {
        let status = match self.context.suspended() {
            Some(interrupt) => WorkflowStatus::AwaitingInput(interrupt),
            None if ok => WorkflowStatus::Completed,
            None => WorkflowStatus::Failed,
        };
        self.journal.set_status(&self.lease, status).await?;
        Ok(())
    }
}

/// Run `workflow` to its value, replaying every call the journal already
/// holds for this workflow id.
///
/// It first claims the workflow (creating it when new), which fences out
/// any process still running it: that process's next journal write fails
/// with a lost lease. Then the function runs on a [`WorkflowContext`]
/// seeded with the stored records. At the end the status is written:
/// `awaiting_input` with the interrupt when one suspended the run (even if
/// the function dropped that error), else `completed` with the value or
/// `failed` with an error. A lost lease writes nothing, since another
/// process owns the workflow now.
///
/// Dropping the returned future stops the workflow where it stands, like a
/// crash: finished calls stay journaled and the status stays `running`.
///
/// Args:
///     options: The journal store, the workflow's key, its limits and the
///         event sink.
///     workflow: The workflow function; it gets the context by value.
///
/// Returns:
///     What `workflow` returned.
///
/// Raises:
///     WorkflowError: `Suspended` at an unanswered interrupt, the
///         function's own error, `Timeout` past the deadline, or `Store`
///         when the claim, a journal write or the final status write fails.
pub async fn run_workflow<T, F, Fut>(
    options: WorkflowOptions,
    workflow: F,
) -> Result<T, WorkflowError>
where
    F: FnOnce(WorkflowContext) -> Fut,
    Fut: Future<Output = Result<T, WorkflowError>>,
{
    let session = WorkflowSession::open(&options).await?;
    let context = session.context().clone();
    let run = workflow(context.clone());
    let outcome = match options.deadline.filter(|deadline| !deadline.is_zero()) {
        Some(after) => tokio::time::timeout(after, run).await.unwrap_or_else(|_| {
            Err(WorkflowError::Timeout {
                label: "workflow".into(),
                after,
            })
        }),
        None => run.await,
    };
    if let Err(WorkflowError::Store(JournalStoreError::LeaseLost { .. })) = outcome {
        return outcome;
    }
    if let Some(interrupt) = context.suspended() {
        session.finish(true).await?;
        return Err(WorkflowError::Suspended(interrupt));
    }
    match outcome {
        Ok(value) => {
            session.finish(true).await?;
            Ok(value)
        }
        // The function's error is the cause; when the status write fails
        // too, the workflow stays `running`, which reads as a crash and
        // resumes the same way.
        Err(error) => {
            let _ = session.finish(false).await;
            Err(error)
        }
    }
}
