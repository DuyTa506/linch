//! [`TaskStore`] for [`PgSessionStore`]: tasks in `linch_tasks`, their
//! edges in `linch_task_edges`.
//!
//! Every write locks the session row first, so the task writes of one
//! session serialize: ids come out in order with no gaps, a claim has one
//! winner, concurrent metadata merges keep every key, and two writes that
//! add edges in opposite orders cannot deadlock. A read is one statement,
//! so a task and its edges come from one snapshot.

use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use linch_core::{
    JsonObject, NewTask, SessionId, StoreError, Task, TaskPatch, TaskStatus, TaskStore,
};
use sqlx::{PgConnection, Postgres, Transaction};

use crate::PgSessionStore;
use crate::codec::{backend, decode_err, encode_err, not_found, to_i64, to_u64};

/// A task row with its edges, as [`select_tasks!`] lists them.
type Row = (
    i64,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    String,
    i64,
    i64,
    Vec<String>,
    Vec<String>,
);

/// The tasks of session `$2` in namespace `$1`, each with its edges in the
/// order they were added, followed by `$rest`.
macro_rules! select_tasks {
    ($rest:literal) => {
        concat!(
            "SELECT t.task_id, t.subject, t.description, t.active_form, t.status, \
             t.owner, t.metadata, t.created_at_ms, t.updated_at_ms, \
             ARRAY(SELECT e.blocked FROM linch_task_edges e \
                   WHERE e.namespace = t.namespace AND e.session_id = t.session_id \
                   AND e.blocker = t.task_id::text ORDER BY e.position), \
             ARRAY(SELECT e.blocker FROM linch_task_edges e \
                   WHERE e.namespace = t.namespace AND e.session_id = t.session_id \
                   AND e.blocked = t.task_id::text ORDER BY e.position) \
             FROM linch_tasks t WHERE t.namespace = $1 AND t.session_id = $2",
            $rest
        )
    };
}

/// The number in a task id as the store hands them out (`1`, `2`, ...).
/// Any other text names no task.
fn number(task_id: &str) -> Option<i64> {
    let canonical = !task_id.starts_with('0') && task_id.bytes().all(|b| b.is_ascii_digit());
    canonical.then(|| task_id.parse().ok()).flatten()
}

/// The wall clock in Unix milliseconds; a clock set before 1970 reads 0.
fn now_ms() -> u64 {
    let since = SystemTime::now().duration_since(UNIX_EPOCH);
    since.map_or(0, |elapsed| {
        u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
    })
}

fn decode(session_id: &SessionId, row: Row) -> Result<Task, StoreError> {
    let (
        id,
        subject,
        description,
        active_form,
        status,
        owner,
        metadata,
        created,
        updated,
        blocks,
        blocked_by,
    ) = row;
    let status = TaskStatus::parse(&status)
        .ok_or_else(|| decode_err("status")(format!("unknown task status {status:?}").into()))?;
    let metadata: JsonObject =
        serde_json::from_str(&metadata).map_err(|e| decode_err("metadata")(e.into()))?;
    Ok(Task {
        id: id.to_string(),
        session_id: session_id.clone(),
        subject,
        description,
        active_form,
        status,
        owner,
        blocks,
        blocked_by,
        metadata,
        created_at_ms: to_u64(created, "created_at_ms")?,
        updated_at_ms: to_u64(updated, "updated_at_ms")?,
    })
}

fn encode_metadata(metadata: &JsonObject) -> Result<String, StoreError> {
    serde_json::to_string(metadata).map_err(|e| encode_err("metadata")(e.into()))
}

async fn fetch(
    conn: &mut PgConnection,
    namespace: &str,
    session_id: &SessionId,
    number: i64,
) -> Result<Option<Task>, StoreError> {
    let row: Option<Row> = sqlx::query_as(select_tasks!(" AND t.task_id = $3"))
        .bind(namespace)
        .bind(session_id.as_str())
        .bind(number)
        .fetch_optional(conn)
        .await
        .map_err(backend)?;
    row.map(|row| decode(session_id, row)).transpose()
}

/// Write the fields of `task` (not its edges) back to row `number`.
async fn save(
    conn: &mut PgConnection,
    namespace: &str,
    number: i64,
    task: &Task,
) -> Result<(), StoreError> {
    sqlx::query(
        "UPDATE linch_tasks SET subject = $4, description = $5, active_form = $6, \
         status = $7, owner = $8, metadata = $9, updated_at_ms = $10 \
         WHERE namespace = $1 AND session_id = $2 AND task_id = $3",
    )
    .bind(namespace)
    .bind(task.session_id.as_str())
    .bind(number)
    .bind(&task.subject)
    .bind(&task.description)
    .bind(&task.active_form)
    .bind(task.status.as_str())
    .bind(&task.owner)
    .bind(encode_metadata(&task.metadata)?)
    .bind(to_i64(task.updated_at_ms, "updated_at_ms")?)
    .execute(conn)
    .await
    .map_err(backend)?;
    Ok(())
}

/// Run one edge statement (`$3` is the task's id, `$4` the other ends),
/// unless there are no other ends.
async fn edges(
    conn: &mut PgConnection,
    sql: &'static str,
    task: &Task,
    namespace: &str,
    others: &[String],
) -> Result<(), StoreError> {
    if others.is_empty() {
        return Ok(());
    }
    sqlx::query(sql)
        .bind(namespace)
        .bind(task.session_id.as_str())
        .bind(&task.id)
        .bind(others)
        .execute(conn)
        .await
        .map_err(backend)?;
    Ok(())
}

/// A transaction holding the lock on a session row, and the number and
/// state of one of its tasks, read under that lock.
type Locked = (Transaction<'static, Postgres>, i64, Task);

impl PgSessionStore {
    /// Lock the session row and read task `task_id`; `None` when the
    /// session or the task is missing.
    async fn lock_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Locked>, StoreError> {
        let Some(number) = number(task_id) else {
            return Ok(None);
        };
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        let locked: Option<i32> = sqlx::query_scalar(
            "SELECT 1 FROM linch_sessions WHERE namespace = $1 AND session_id = $2 \
             FOR NO KEY UPDATE",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .fetch_optional(&mut *tx)
        .await
        .map_err(backend)?;
        if locked.is_none() {
            return Ok(None);
        }
        let task = fetch(&mut tx, namespace, session_id, number).await?;
        Ok(task.map(|task| (tx, number, task)))
    }
}

#[async_trait]
impl TaskStore for PgSessionStore {
    async fn create_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task: NewTask,
    ) -> Result<Task, StoreError> {
        let metadata = encode_metadata(&task.metadata)?;
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        // Takes the session row's lock and the next id in one statement.
        let number: Option<i64> = sqlx::query_scalar(
            "UPDATE linch_sessions SET next_task_id = next_task_id + 1 \
             WHERE namespace = $1 AND session_id = $2 RETURNING next_task_id - 1",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .fetch_optional(&mut *tx)
        .await
        .map_err(backend)?;
        let number = number.ok_or_else(|| not_found("session", namespace, session_id.as_str()))?;
        let now = to_i64(now_ms(), "created_at_ms")?;
        sqlx::query(
            "INSERT INTO linch_tasks (namespace, session_id, task_id, subject, description, \
             active_form, status, owner, metadata, created_at_ms, updated_at_ms) \
             VALUES ($1, $2, $3, $4, $5, $6, 'pending', NULL, $7, $8, $8)",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .bind(number)
        .bind(task.subject)
        .bind(task.description)
        .bind(task.active_form)
        .bind(metadata)
        .bind(now)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        let created = fetch(&mut tx, namespace, session_id, number).await?;
        let created = created.ok_or_else(|| not_found("task", namespace, &number.to_string()))?;
        tx.commit().await.map_err(backend)?;
        Ok(created)
    }

    async fn get_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError> {
        let Some(number) = number(task_id) else {
            return Ok(None);
        };
        let mut conn = self.pool.acquire().await.map_err(backend)?;
        fetch(&mut conn, namespace, session_id, number).await
    }

    async fn list_tasks(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<Task>, StoreError> {
        let rows: Vec<Row> = sqlx::query_as(select_tasks!(" ORDER BY t.task_id"))
            .bind(namespace)
            .bind(session_id.as_str())
            .fetch_all(&self.pool)
            .await
            .map_err(backend)?;
        rows.into_iter()
            .map(|row| decode(session_id, row))
            .collect()
    }

    async fn update_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        patch: TaskPatch,
    ) -> Result<Option<Task>, StoreError> {
        let Some((mut tx, number, mut task)) =
            self.lock_task(namespace, session_id, task_id).await?
        else {
            return Ok(None);
        };
        patch.apply_fields(&mut task);
        task.updated_at_ms = now_ms();
        save(&mut tx, namespace, number, &task).await?;
        let (ns, conn) = (namespace, &mut *tx);
        let unblock = "DELETE FROM linch_task_edges WHERE namespace = $1 AND session_id = $2 \
                       AND blocker = $3 AND blocked = ANY($4)";
        edges(conn, unblock, &task, ns, &patch.remove_blocks).await?;
        let unblocked = "DELETE FROM linch_task_edges WHERE namespace = $1 AND session_id = $2 \
                         AND blocked = $3 AND blocker = ANY($4)";
        edges(conn, unblocked, &task, ns, &patch.remove_blocked_by).await?;
        let block = "INSERT INTO linch_task_edges (namespace, session_id, blocker, blocked) \
                     SELECT $1, $2, $3, other FROM unnest($4::text[]) WITH ORDINALITY \
                     AS o(other, n) ORDER BY n ON CONFLICT DO NOTHING";
        edges(conn, block, &task, ns, &patch.add_blocks).await?;
        let blocked = "INSERT INTO linch_task_edges (namespace, session_id, blocker, blocked) \
                       SELECT $1, $2, other, $3 FROM unnest($4::text[]) WITH ORDINALITY \
                       AS o(other, n) ORDER BY n ON CONFLICT DO NOTHING";
        edges(conn, blocked, &task, ns, &patch.add_blocked_by).await?;
        let updated = fetch(conn, namespace, session_id, number).await?;
        tx.commit().await.map_err(backend)?;
        Ok(updated)
    }

    async fn delete_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<bool, StoreError> {
        let Some((mut tx, number, task)) = self.lock_task(namespace, session_id, task_id).await?
        else {
            return Ok(false);
        };
        sqlx::query(
            "DELETE FROM linch_task_edges WHERE namespace = $1 AND session_id = $2 \
             AND (blocker = $3 OR blocked = $3)",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .bind(&task.id)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        sqlx::query(
            "DELETE FROM linch_tasks WHERE namespace = $1 AND session_id = $2 AND task_id = $3",
        )
        .bind(namespace)
        .bind(session_id.as_str())
        .bind(number)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(true)
    }

    async fn claim_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
        owner: &str,
    ) -> Result<Option<Task>, StoreError> {
        let Some((mut tx, number, mut task)) =
            self.lock_task(namespace, session_id, task_id).await?
        else {
            return Ok(None);
        };
        if task.owner.is_some() || task.status != TaskStatus::Pending {
            return Ok(None);
        }
        task.owner = Some(owner.to_owned());
        task.updated_at_ms = now_ms();
        save(&mut tx, namespace, number, &task).await?;
        tx.commit().await.map_err(backend)?;
        Ok(Some(task))
    }

    async fn release_task(
        &self,
        namespace: &str,
        session_id: &SessionId,
        task_id: &str,
    ) -> Result<Option<Task>, StoreError> {
        let Some((mut tx, number, mut task)) =
            self.lock_task(namespace, session_id, task_id).await?
        else {
            return Ok(None);
        };
        if task.status == TaskStatus::Completed {
            return Ok(Some(task));
        }
        task.owner = None;
        task.status = TaskStatus::Pending;
        task.updated_at_ms = now_ms();
        save(&mut tx, namespace, number, &task).await?;
        tx.commit().await.map_err(backend)?;
        Ok(Some(task))
    }
}
