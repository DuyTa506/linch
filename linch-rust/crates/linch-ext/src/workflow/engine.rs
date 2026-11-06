//! [`run_workflow`] and its [`WorkflowOptions`].

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use linch_core::JsonValue;
use tokio_util::sync::CancellationToken;

use super::context::{EventSink, WorkflowContext};
use super::error::WorkflowError;
use super::journal::{JournalStore, JournalStoreError, WorkflowStatus};

/// How one [`run_workflow`] call journals and runs.
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
    /// used once, then journaled.
    pub resume: HashMap<String, JsonValue>,
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
}

impl WorkflowOptions {
    /// Options for workflow `workflow_id` of `namespace`, journaled in
    /// `journal`, with no answers, no timeouts, a fresh cancel token and no
    /// event sink.
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
            step_timeout: None,
            deadline: None,
            cancel: CancellationToken::new(),
            on_event: None,
        }
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
    let journal = options.journal.clone();
    let lease = (journal.claim(&options.namespace, &options.workflow_id)).await?;
    let stored = (journal.load(&options.namespace, &options.workflow_id)).await?;
    let records = stored.map(|run| run.records).unwrap_or_default();
    let context = WorkflowContext::new(lease.clone(), records, &options);
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
        let status = WorkflowStatus::AwaitingInput(interrupt.clone());
        journal.set_status(&lease, status).await?;
        return Err(WorkflowError::Suspended(interrupt));
    }
    match outcome {
        Ok(value) => {
            journal
                .set_status(&lease, WorkflowStatus::Completed)
                .await?;
            Ok(value)
        }
        // The function's error is the cause; when the status write fails
        // too, the workflow stays `running`, which reads as a crash and
        // resumes the same way.
        Err(error) => {
            let _ = journal.set_status(&lease, WorkflowStatus::Failed).await;
            Err(error)
        }
    }
}
