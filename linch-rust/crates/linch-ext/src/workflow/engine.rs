//! [`run_workflow`] and its [`WorkflowOptions`].

use std::future::Future;
use std::sync::Arc;

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
    /// Receives every workflow event; `None` drops them.
    pub on_event: Option<EventSink>,
}

impl WorkflowOptions {
    /// Options for workflow `workflow_id` of `namespace`, journaled in
    /// `journal`, with no event sink.
    pub fn new(
        journal: Arc<dyn JournalStore>,
        namespace: impl Into<String>,
        workflow_id: impl Into<String>,
    ) -> Self {
        Self {
            journal,
            namespace: namespace.into(),
            workflow_id: workflow_id.into(),
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
/// `completed` with the value, `failed` with an error. A lost lease writes
/// nothing, since another process owns the workflow now.
///
/// Dropping the returned future stops the workflow where it stands, like a
/// crash: finished calls stay journaled and the status stays `running`.
///
/// Args:
///     options: The journal store, the workflow's key and the event sink.
///     workflow: The workflow function; it gets the context by value.
///
/// Returns:
///     What `workflow` returned.
///
/// Raises:
///     WorkflowError: The function's own error, or `Store` when the
///         claim, a journal write or the final status write fails.
pub async fn run_workflow<T, F, Fut>(
    options: WorkflowOptions,
    workflow: F,
) -> Result<T, WorkflowError>
where
    F: FnOnce(WorkflowContext) -> Fut,
    Fut: Future<Output = Result<T, WorkflowError>>,
{
    let WorkflowOptions {
        journal,
        namespace,
        workflow_id,
        on_event,
    } = options;
    let lease = journal.claim(&namespace, &workflow_id).await?;
    let stored = journal.load(&namespace, &workflow_id).await?;
    let records = stored.map(|run| run.records).unwrap_or_default();
    let context = WorkflowContext::new(journal.clone(), lease.clone(), records, on_event);
    let outcome = workflow(context).await;
    match outcome {
        Err(WorkflowError::Store(JournalStoreError::LeaseLost { .. })) => outcome,
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
