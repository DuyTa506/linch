//! [`TaskLedgerContext`]: shows the model the open tasks of a shared task
//! list with every request, so a session that starts with an empty
//! conversation reads where the work stands (see [`crate::task_tools_in`]).

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{ContextBuild, ContextBuildRequest, ContextBuildResponse};
use linch_core::{CallbackError, Hook, SessionId, TaskStatus, TaskStore, TextBlock};

/// Stable id of the hook's policy, in the run contract. Name it in
/// `context_budget.pinned` to keep the ledger when a budget drops blocks.
pub const LEDGER_POLICY_ID: &str = "linch.tasks.ledger-context";

/// Characters of ledger text shown unless the config says otherwise.
pub const DEFAULT_LEDGER_CHARS: usize = 4000;

/// A `context_build` hook that adds the open tasks of one task list to every
/// request: one `#id [status] subject` line each, in id order, then how many
/// are completed. It stops at `max_chars` and says how many open tasks it
/// left out.
///
/// The block joins the tail of the request only: it is never logged, and
/// the cached prefix stays byte-identical. The store is read again for each
/// request, so a change the model made shows on its next turn. Nothing is
/// written. A failed read is a hook error, which the chain skips (the point
/// fails open).
pub struct TaskLedgerContext {
    store: Arc<dyn TaskStore>,
    namespace: String,
    list: SessionId,
    max_chars: usize,
}

impl TaskLedgerContext {
    /// A hook that reads the tasks of `list` in `namespace` from `store`.
    pub fn new(
        store: Arc<dyn TaskStore>,
        namespace: &str,
        list: SessionId,
        max_chars: usize,
    ) -> Self {
        Self {
            store,
            namespace: namespace.to_owned(),
            list,
            max_chars,
        }
    }
}

const HEADER: &str = "Task ledger: kept outside the conversation, so it survives context \
     resets and new sessions. Read it first, work on one open task, and keep it current with \
     the task tools.";

#[async_trait]
impl Hook<ContextBuild> for TaskLedgerContext {
    fn policy_id(&self) -> &str {
        LEDGER_POLICY_ID
    }

    fn policy_version(&self) -> u32 {
        1
    }

    async fn call(
        &self,
        _request: &ContextBuildRequest,
    ) -> Result<ContextBuildResponse, CallbackError> {
        let tasks = self.store.list_tasks(&self.namespace, &self.list).await;
        let tasks = tasks.map_err(|error| -> CallbackError { Box::new(error) })?;
        let done = tasks
            .iter()
            .filter(|task| task.status == TaskStatus::Completed)
            .count();
        let mut text = String::from(HEADER);
        let open: Vec<_> = (tasks.iter())
            .filter(|task| task.status != TaskStatus::Completed)
            .collect();
        if open.is_empty() && done == 0 {
            text.push_str("\nThe ledger is empty.");
        }
        let mut shown = 0;
        for task in &open {
            let line = format!("\n#{} [{}] {}", task.id, task.status.as_str(), task.subject);
            if text.len() + line.len() > self.max_chars {
                break;
            }
            text.push_str(&line);
            shown += 1;
        }
        if shown < open.len() {
            let left = open.len() - shown;
            text.push_str(&format!(
                "\n[{left} more open tasks not shown; call TaskList.]"
            ));
        }
        if done > 0 {
            text.push_str(&format!("\n({done} completed)"));
        }
        Ok(ContextBuildResponse {
            blocks: vec![TextBlock { text }],
            ..ContextBuildResponse::default()
        })
    }
}
