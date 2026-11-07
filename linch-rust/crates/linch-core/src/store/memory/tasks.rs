//! [`TaskStore`] for [`MemorySessionStore`]: one board of tasks and edges
//! per session, behind the store's lock, so every call is atomic.

use async_trait::async_trait;

use super::{MemorySessionStore, SessionState, lock, not_found};
use crate::events::now_ms;
use crate::ids::SessionId;
use crate::store::{NewTask, StoreError, Task, TaskPatch, TaskStatus, TaskStore};

/// One session's tasks.
#[derive(Debug)]
pub(super) struct Board {
    next_id: u64,
    /// In id order, which is creation order; `blocks` and `blocked_by` are
    /// left empty and filled from `edges` on the way out.
    tasks: Vec<Task>,
    /// `(blocker, blocked)` pairs, oldest first.
    edges: Vec<(String, String)>,
}

impl Default for Board {
    fn default() -> Self {
        Self {
            next_id: 1,
            tasks: Vec::new(),
            edges: Vec::new(),
        }
    }
}

impl Board {
    fn index(&self, task_id: &str) -> Option<usize> {
        self.tasks.iter().position(|task| task.id == task_id)
    }

    /// A copy of `self.tasks[index]` with its edges.
    fn view(&self, index: usize) -> Task {
        let mut task = self.tasks[index].clone();
        for (blocker, blocked) in &self.edges {
            if *blocker == task.id {
                task.blocks.push(blocked.clone());
            }
            if *blocked == task.id {
                task.blocked_by.push(blocker.clone());
            }
        }
        task
    }

    fn add_edge(&mut self, blocker: &str, blocked: &str) {
        let edge = (blocker.to_owned(), blocked.to_owned());
        if !self.edges.contains(&edge) {
            self.edges.push(edge);
        }
    }

    fn apply_edges(&mut self, task_id: &str, patch: &TaskPatch) {
        self.edges.retain(|(blocker, blocked)| {
            let unblock = *blocker == task_id && patch.remove_blocks.contains(blocked);
            let unblocked = *blocked == task_id && patch.remove_blocked_by.contains(blocker);
            !(unblock || unblocked)
        });
        for blocked in &patch.add_blocks {
            self.add_edge(task_id, blocked);
        }
        for blocker in &patch.add_blocked_by {
            self.add_edge(blocker, task_id);
        }
    }
}

impl SessionState {
    fn board(&mut self, namespace: &str, session_id: &SessionId) -> Option<&mut Board> {
        (self.boards.get_mut(namespace)).and_then(|space| space.get_mut(session_id))
    }
}

/// Run `change` on task `task_id` of the session's board and return the
/// task with its edges, or `None` when there is no such task.
fn with_task(
    store: &MemorySessionStore,
    namespace: &str,
    session_id: &SessionId,
    task_id: &str,
    change: impl FnOnce(&mut Board, usize),
) -> Option<Task> {
    let mut state = lock(&store.state);
    let board = state.board(namespace, session_id)?;
    let index = board.index(task_id)?;
    change(board, index);
    Some(board.view(index))
}

#[async_trait]
impl TaskStore for MemorySessionStore {
    async fn create_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task: NewTask,
    ) -> Result<Task, StoreError> {
        let mut state = lock(&self.state);
        let exists =
            (state.logs.get(namespace)).is_some_and(|space| space.contains_key(session_id));
        if !exists {
            return Err(not_found("session", namespace, session_id.as_str()));
        }
        let space = state.boards.entry(namespace.to_owned()).or_default();
        let board = space.entry(session_id.clone()).or_default();
        let now = now_ms();
        board.tasks.push(Task {
            id: board.next_id.to_string(),
            session_id: session_id.clone(),
            subject: task.subject,
            description: task.description,
            active_form: task.active_form,
            status: TaskStatus::Pending,
            owner: None,
            blocks: Vec::new(),
            blocked_by: Vec::new(),
            metadata: task.metadata,
            created_at_ms: now,
            updated_at_ms: now,
        });
        board.next_id += 1;
        Ok(board.view(board.tasks.len() - 1))
    }

    async fn get_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError> {
        Ok(with_task(self, namespace, session_id, task_id, |_, _| {}))
    }

    async fn list_tasks(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<Task>, StoreError> {
        let mut state = lock(&self.state);
        let Some(board) = state.board(namespace, session_id) else {
            return Ok(Vec::new());
        };
        Ok((0..board.tasks.len())
            .map(|index| board.view(index))
            .collect())
    }

    async fn update_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        patch: TaskPatch,
    ) -> Result<Option<Task>, StoreError> {
        let updated = with_task(self, namespace, session_id, task_id, |board, index| {
            let task = &mut board.tasks[index];
            patch.apply_fields(task);
            task.updated_at_ms = now_ms();
            board.apply_edges(task_id, &patch);
        });
        Ok(updated)
    }

    async fn delete_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<bool, StoreError> {
        let mut state = lock(&self.state);
        let Some(board) = state.board(namespace, session_id) else {
            return Ok(false);
        };
        let Some(index) = board.index(task_id) else {
            return Ok(false);
        };
        board.tasks.remove(index);
        board.edges.retain(|(a, b)| a != task_id && b != task_id);
        Ok(true)
    }

    async fn claim_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        owner: &str,
    ) -> Result<Option<Task>, StoreError> {
        let mut claimed = false;
        let task = with_task(self, namespace, session_id, task_id, |board, index| {
            let task = &mut board.tasks[index];
            if task.owner.is_none() && task.status == TaskStatus::Pending {
                task.owner = Some(owner.to_owned());
                task.updated_at_ms = now_ms();
                claimed = true;
            }
        });
        Ok(task.filter(|_| claimed))
    }

    async fn release_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError> {
        let released = with_task(self, namespace, session_id, task_id, |board, index| {
            let task = &mut board.tasks[index];
            if task.status != TaskStatus::Completed {
                task.owner = None;
                task.status = TaskStatus::Pending;
                task.updated_at_ms = now_ms();
            }
        });
        Ok(released)
    }
}
