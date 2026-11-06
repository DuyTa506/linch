//! Session tasks: a small dependency graph of work items kept per session.
//!
//! Tasks are session data, not log entries: the model reaches them through
//! the `TaskCreate`, `TaskGet`, `TaskList` and `TaskUpdate` tools, and only
//! those calls and their results enter the session log.
//!
//! An edge "`a` blocks `b`" shows up as `b` in `a`'s [`Task::blocks`] and as
//! `a` in `b`'s [`Task::blocked_by`]. Either end may name a task that does
//! not exist yet: the edge is kept and shows up on that task once it is
//! created. Deleting a task removes every edge that names it.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::ids::SessionId;
use crate::json::{JsonObject, JsonValue};
use crate::store::StoreError;

/// Lifecycle state of a task.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    /// Not started. Every task starts here.
    #[default]
    Pending,
    /// Being worked on.
    InProgress,
    /// Done.
    Completed,
}

impl TaskStatus {
    /// The wire name: `pending`, `in_progress` or `completed`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::InProgress => "in_progress",
            Self::Completed => "completed",
        }
    }

    /// The status with wire name `name`, if there is one.
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "pending" => Some(Self::Pending),
            "in_progress" => Some(Self::InProgress),
            "completed" => Some(Self::Completed),
            _ => None,
        }
    }
}

/// One session task and its dependency edges.
#[derive(Debug, Clone, PartialEq)]
pub struct Task {
    /// Number in decimal, handed out per session from 1 up and never reused.
    pub id: String,
    /// Owning session.
    pub session_id: SessionId,
    /// Short title.
    pub subject: String,
    /// What needs to be done.
    pub description: String,
    /// Present-tense label shown while the task is active.
    pub active_form: Option<String>,
    /// Lifecycle state.
    pub status: TaskStatus,
    /// Worker or caller that claimed the task.
    pub owner: Option<String>,
    /// Ids this task blocks, oldest edge first.
    pub blocks: Vec<String>,
    /// Ids that block this task, oldest edge first.
    pub blocked_by: Vec<String>,
    /// Application-defined data.
    pub metadata: JsonObject,
    /// Creation time, in milliseconds since the Unix epoch.
    pub created_at_ms: u64,
    /// Last change to the task's own fields, in milliseconds since the Unix
    /// epoch. Edge changes made from the other end do not count.
    pub updated_at_ms: u64,
}

/// The fields of a task to create. It starts `pending` and unowned.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NewTask {
    /// Short title.
    pub subject: String,
    /// What needs to be done.
    pub description: String,
    /// Present-tense label shown while the task is active.
    pub active_form: Option<String>,
    /// Application-defined data.
    pub metadata: JsonObject,
}

impl NewTask {
    /// A task with `subject` and `description` and nothing else.
    pub fn new(subject: impl Into<String>, description: impl Into<String>) -> Self {
        Self {
            subject: subject.into(),
            description: description.into(),
            ..Self::default()
        }
    }
}

/// A partial update of one task. Empty or `None` fields change nothing.
///
/// Edge removals apply before additions, so an id both removed and added
/// is present afterwards; adding an edge twice or removing a missing one
/// changes nothing. The owner cannot be cleared here: see
/// [`TaskStore::release_task`].
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TaskPatch {
    /// New title.
    pub subject: Option<String>,
    /// New description.
    pub description: Option<String>,
    /// New active label.
    pub active_form: Option<String>,
    /// New status.
    pub status: Option<TaskStatus>,
    /// New owner.
    pub owner: Option<String>,
    /// Ids this task starts blocking.
    pub add_blocks: Vec<String>,
    /// Ids that start blocking this task.
    pub add_blocked_by: Vec<String>,
    /// Ids this task stops blocking.
    pub remove_blocks: Vec<String>,
    /// Ids that stop blocking this task.
    pub remove_blocked_by: Vec<String>,
    /// Metadata merged by key; a `null` value removes that key.
    pub metadata: JsonObject,
}

impl TaskPatch {
    /// Apply the field changes (not the edges) to `task`. Store
    /// implementations call this so every store merges the same way; the
    /// caller sets `updated_at_ms`.
    pub fn apply_fields(&self, task: &mut Task) {
        let replace = |field: &mut String, value: &Option<String>| {
            if let Some(value) = value {
                field.clone_from(value);
            }
        };
        replace(&mut task.subject, &self.subject);
        replace(&mut task.description, &self.description);
        if let Some(active_form) = &self.active_form {
            task.active_form = Some(active_form.clone());
        }
        if let Some(status) = self.status {
            task.status = status;
        }
        if let Some(owner) = &self.owner {
            task.owner = Some(owner.clone());
        }
        for (key, value) in &self.metadata {
            match value {
                JsonValue::Null => task.metadata.shift_remove(key),
                value => task.metadata.insert(key.clone(), value.clone()),
            };
        }
    }
}

/// Whether `task` is pending, unowned, and every id in its `blocked_by` is
/// a task in `all` that is completed.
fn is_ready(task: &Task, all: &[Task]) -> bool {
    task.status == TaskStatus::Pending
        && task.owner.is_none()
        && task.blocked_by.iter().all(|id| {
            all.iter()
                .any(|dep| dep.id == *id && dep.status == TaskStatus::Completed)
        })
}

/// Durable session tasks, keyed like sessions by namespace and session id.
///
/// Implemented by the session stores ([`MemorySessionStore`] and the
/// Postgres one), which pass [`super::conformance::task_store_suite`]. Every
/// write is atomic per session; returned tasks are copies.
///
/// [`MemorySessionStore`]: super::memory::MemorySessionStore
#[async_trait]
pub trait TaskStore: Send + Sync {
    /// Create a `pending`, unowned task with the session's next id. Edges
    /// that already name that id show up on it.
    ///
    /// Raises:
    ///     StoreError: `NotFound` when the session does not exist.
    async fn create_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task: NewTask,
    ) -> Result<Task, StoreError>;

    /// The task, or `None` when the session has no task `task_id`.
    async fn get_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError>;

    /// Every task of the session in id order; empty for a missing session.
    async fn list_tasks(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<Task>, StoreError>;

    /// Apply `patch` and return the updated task, or `None` when there is
    /// no such task.
    async fn update_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        patch: TaskPatch,
    ) -> Result<Option<Task>, StoreError>;

    /// Delete the task and every edge that names it. Returns whether a task
    /// was deleted; for a missing task nothing changes.
    async fn delete_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<bool, StoreError>;

    /// Give a pending, unowned task to `owner`, leaving its status. `None`
    /// when the task is missing, owned or not pending; of concurrent claims
    /// exactly one wins.
    async fn claim_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        owner: &str,
    ) -> Result<Option<Task>, StoreError>;

    /// Clear the owner of an unfinished task and set it back to `pending`.
    /// A completed task comes back unchanged; `None` when it is missing.
    async fn release_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError>;

    /// Pending, unowned tasks whose blockers all exist and are completed,
    /// in id order.
    async fn ready_tasks(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<Task>, StoreError> {
        let all = self.list_tasks(namespace, session_id).await?;
        let ready = all.iter().filter(|task| is_ready(task, &all));
        Ok(ready.cloned().collect())
    }
}
