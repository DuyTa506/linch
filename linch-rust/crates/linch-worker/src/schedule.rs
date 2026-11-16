//! Schedules that enqueue new runs: a cron pattern in a time zone, or a
//! fixed interval.
//!
//! [`fire_due_schedules`] locks the due rows with `FOR UPDATE SKIP LOCKED`,
//! enqueues one start job per row and moves `next_fire` forward, all in one
//! transaction. A fire therefore happens once however many workers poll,
//! and a crash before the commit leaves neither the job nor the move: a
//! second poller skips the locked rows, and after the commit the rows are
//! no longer due. Each job also carries the dedupe key
//! `schedule:{id}:{fire time}`, a second guard against a double fire.
//!
//! Missed fires are caught up, as Python Linch does: the next fire is the
//! first tick after the fire just made, not after now, so a schedule that
//! was overdue by several ticks (every worker was down) fires each of them
//! in order, one per call. A schedule with a fixed session skips a fire
//! while an earlier fire's job is unfinished or the session has a live run,
//! so its catch-up collapses into one run. Resuming a paused schedule
//! starts from now: the fires it missed while paused are not made.

use chrono::{DateTime, SecondsFormat, Utc};
use linch_core::SessionId;
use linch_postgres::PgPool;
use sqlx::{Postgres, Transaction};

use crate::definition::{self, Cap, Counted};
use crate::error::{WorkerError, chain, non_blank};
use crate::job::{Inserted, NewJob, insert};
use crate::timing::Timing;

/// Most due schedules one [`fire_due_schedules`] call fires.
const FIRE_BATCH: i64 = 64;

/// A schedule that enqueues a run of `prompt` at every fire.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    /// Store namespace of the agent that runs the fires; schedule ids are
    /// unique within it.
    pub namespace: String,
    /// Schedule id.
    pub id: String,
    /// Queue the fires go to.
    pub queue: String,
    /// The session every fire's run continues (a heartbeat). `None`: each
    /// fire starts the session `schedule:{id}:{fire time}`.
    pub session_id: Option<SessionId>,
    /// The user prompt of every fire's run.
    pub prompt: String,
    /// When it fires.
    pub timing: Timing,
    /// The agent definition every fire runs with, stored with the
    /// schedule (see [`crate::AgentFactory`]). `None`: the worker's agent
    /// for the namespace.
    pub agent: Option<String>,
}

/// A stored schedule and what it did so far.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleInfo {
    /// Its namespace.
    pub namespace: String,
    /// Its id.
    pub id: String,
    /// Its queue.
    pub queue: String,
    /// The session of every fire, or `None` for a new one each fire.
    pub session_id: Option<SessionId>,
    /// The prompt of every fire.
    pub prompt: String,
    /// When it fires.
    pub timing: Timing,
    /// Digest of its stored agent definition, if it has one.
    pub agent: Option<String>,
    /// Whether it is paused: a paused schedule does not fire.
    pub paused: bool,
    /// Its next fire (while paused, the one it had when paused).
    pub next_fire: DateTime<Utc>,
    /// The time of its latest fire, made or skipped.
    pub last_fire: Option<DateTime<Utc>>,
    /// Fires that enqueued a job.
    pub fires: u64,
    /// Fires skipped because the session was busy.
    pub skips: u64,
    /// When it was created.
    pub created_at: DateTime<Utc>,
}

/// The columns of a [`Row`], in order.
macro_rules! columns {
    () => {
        "namespace, id, queue, session_id, prompt, cron, interval_ms, timezone, \
         agent, enabled, next_fire, last_fire, fires, skips, created_at"
    };
}

type Row = (
    String,
    String,
    String,
    Option<String>,
    String,
    Option<String>,
    Option<i64>,
    String,
    Option<String>,
    bool,
    DateTime<Utc>,
    Option<DateTime<Utc>>,
    i64,
    i64,
    DateTime<Utc>,
);

fn info(row: Row) -> ScheduleInfo {
    let (namespace, id, queue, session_id, prompt, cron, interval_ms, timezone) =
        (row.0, row.1, row.2, row.3, row.4, row.5, row.6, row.7);
    ScheduleInfo {
        namespace,
        id,
        queue,
        session_id: session_id.map(SessionId::new),
        prompt,
        timing: timing(cron, interval_ms, timezone),
        agent: row.8,
        paused: !row.9,
        next_fire: row.10,
        last_fire: row.11,
        fires: u64::try_from(row.12).unwrap_or(0),
        skips: u64::try_from(row.13).unwrap_or(0),
        created_at: row.14,
    }
}

/// The timing of a row; the check constraint sets exactly one of `cron`
/// and `interval_ms`.
fn timing(cron: Option<String>, interval_ms: Option<i64>, timezone: String) -> Timing {
    match cron {
        Some(pattern) => Timing::Cron { pattern, timezone },
        None => {
            let ms = u64::try_from(interval_ms.unwrap_or(1)).unwrap_or(1);
            Timing::Every(std::time::Duration::from_millis(ms))
        }
    }
}

/// The columns of a timing: `(cron, interval_ms, timezone)`.
fn columns(timing: &Timing) -> Result<(Option<&str>, Option<i64>, &str), WorkerError> {
    match timing {
        Timing::Cron { pattern, timezone } => Ok((Some(pattern), None, timezone)),
        Timing::Every(every) => {
            let ms = i64::try_from(every.as_millis())
                .map_err(|_| WorkerError::Invalid("the interval is too long".into()))?;
            Ok((None, Some(ms), "UTC"))
        }
    }
}

async fn now(tx: &mut Transaction<'static, Postgres>) -> Result<DateTime<Utc>, WorkerError> {
    Ok(sqlx::query_scalar("SELECT now()")
        .fetch_one(&mut **tx)
        .await?)
}

/// Store `schedule`, its first fire being the first tick after the
/// database's current time.
///
/// Args:
///     pool: Pool for the queue's database.
///     schedule: The schedule; its `(namespace, id)` must be new.
///     cap: The most schedules the namespace's root may hold, counted in
///         the same transaction; `None` for no cap.
///
/// Returns:
///     The stored schedule.
///
/// Raises:
///     WorkerError: `ScheduleExists` if the id is taken, `LimitReached` at
///         the cap; otherwise if a name is blank, the timing is invalid or
///         never fires, or a query fails.
pub async fn create_schedule(
    pool: &PgPool,
    schedule: &Schedule,
    cap: Option<&Cap>,
) -> Result<ScheduleInfo, WorkerError> {
    non_blank(&schedule.namespace, "the namespace")?;
    non_blank(&schedule.id, "the schedule id")?;
    non_blank(&schedule.queue, "the queue")?;
    if let Some(session) = &schedule.session_id {
        non_blank(session.as_str(), "the session id")?;
    }
    let parsed = schedule.timing.parse()?;
    let (cron, interval_ms, timezone) = columns(&schedule.timing)?;
    let mut tx = crate::begin(pool).await?;
    definition::check(&mut tx, cap, Counted::Schedules).await?;
    let first = parsed.next_after(&schedule.timing, now(&mut tx).await?)?;
    let agent = match &schedule.agent {
        Some(config) => Some(definition::store(&mut tx, &schedule.namespace, config).await?),
        None => None,
    };
    let row: Result<Row, sqlx::Error> = sqlx::query_as(concat!(
        "INSERT INTO linch_worker_schedules \
         (namespace, id, queue, session_id, prompt, cron, interval_ms, timezone, agent, next_fire) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING ",
        columns!(),
        ""
    ))
    .bind(&schedule.namespace)
    .bind(&schedule.id)
    .bind(&schedule.queue)
    .bind(schedule.session_id.as_ref().map(SessionId::as_str))
    .bind(&schedule.prompt)
    .bind(cron)
    .bind(interval_ms)
    .bind(timezone)
    .bind(agent)
    .bind(first)
    .fetch_one(&mut *tx)
    .await;
    let row = match row {
        Ok(row) => row,
        Err(error) if unique_violation(&error) => {
            return Err(WorkerError::ScheduleExists {
                id: schedule.id.clone(),
            });
        }
        Err(error) => return Err(error.into()),
    };
    tx.commit().await?;
    Ok(info(row))
}

fn unique_violation(error: &sqlx::Error) -> bool {
    let code = error.as_database_error().and_then(|error| error.code());
    code.as_deref() == Some("23505")
}

/// The schedules of `namespace`, by id.
///
/// Raises:
///     WorkerError: If the query fails.
pub async fn list_schedules(
    pool: &PgPool,
    namespace: &str,
) -> Result<Vec<ScheduleInfo>, WorkerError> {
    let rows: Vec<Row> = sqlx::query_as(concat!(
        "SELECT ",
        columns!(),
        " FROM linch_worker_schedules WHERE namespace = $1 ORDER BY id"
    ))
    .bind(namespace)
    .fetch_all(pool)
    .await?;
    Ok(rows.into_iter().map(info).collect())
}

/// One schedule of `namespace`, or `None`.
///
/// Raises:
///     WorkerError: If the query fails.
pub async fn get_schedule(
    pool: &PgPool,
    namespace: &str,
    id: &str,
) -> Result<Option<ScheduleInfo>, WorkerError> {
    let row: Option<Row> = sqlx::query_as(concat!(
        "SELECT ",
        columns!(),
        " FROM linch_worker_schedules WHERE namespace = $1 AND id = $2"
    ))
    .bind(namespace)
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(info))
}

/// Stop a schedule's fires until [`resume_schedule`]. Runs it already
/// enqueued stay queued. Pausing a paused schedule changes nothing.
///
/// Returns:
///     The schedule, or `None` if `namespace` has no schedule `id`.
///
/// Raises:
///     WorkerError: If the update fails.
pub async fn pause_schedule(
    pool: &PgPool,
    namespace: &str,
    id: &str,
) -> Result<Option<ScheduleInfo>, WorkerError> {
    let row: Option<Row> = sqlx::query_as(concat!(
        "UPDATE linch_worker_schedules SET enabled = FALSE \
         WHERE namespace = $1 AND id = $2 RETURNING ",
        columns!(),
        ""
    ))
    .bind(namespace)
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(info))
}

/// Let a paused schedule fire again, from the first tick after now: the
/// fires it missed while paused are not made. Resuming a running schedule
/// changes nothing.
///
/// Returns:
///     The schedule, or `None` if `namespace` has no schedule `id`.
///
/// Raises:
///     WorkerError: If the stored timing no longer parses, or a query fails.
pub async fn resume_schedule(
    pool: &PgPool,
    namespace: &str,
    id: &str,
) -> Result<Option<ScheduleInfo>, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    let row: Option<Row> = sqlx::query_as(concat!(
        "SELECT ",
        columns!(),
        " FROM linch_worker_schedules \
         WHERE namespace = $1 AND id = $2 FOR UPDATE"
    ))
    .bind(namespace)
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else { return Ok(None) };
    let current = info(row);
    if !current.paused {
        return Ok(Some(current));
    }
    let next = current.timing.next_after(now(&mut tx).await?)?;
    let row: Row = sqlx::query_as(concat!(
        "UPDATE linch_worker_schedules SET enabled = TRUE, next_fire = $3 \
         WHERE namespace = $1 AND id = $2 RETURNING ",
        columns!(),
        ""
    ))
    .bind(namespace)
    .bind(id)
    .bind(next)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Some(info(row)))
}

/// Delete a schedule. Its jobs, queued or running, are left alone: a run
/// already in progress finishes, and a queued fire still runs.
///
/// Returns:
///     Whether `namespace` had a schedule `id`.
///
/// Raises:
///     WorkerError: If the delete fails.
pub async fn delete_schedule(
    pool: &PgPool,
    namespace: &str,
    id: &str,
) -> Result<bool, WorkerError> {
    let done = sqlx::query("DELETE FROM linch_worker_schedules WHERE namespace = $1 AND id = $2")
        .bind(namespace)
        .bind(id)
        .execute(pool)
        .await?;
    Ok(done.rows_affected() == 1)
}

/// Fire every due schedule once: enqueue its start job (or skip the fire
/// while its fixed session is busy) and move its next fire to the first
/// tick after this fire. Workers call this once per poll interval; a host
/// may call it too.
///
/// A stored timing that no longer parses pauses its schedule (logged)
/// instead of blocking the others.
///
/// Args:
///     pool: Pool for the queue's database.
///
/// Returns:
///     How many jobs this call enqueued.
///
/// Raises:
///     WorkerError: If a query fails; nothing of the call is kept then.
pub async fn fire_due_schedules(pool: &PgPool) -> Result<u64, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    let due: Vec<Row> = sqlx::query_as(concat!(
        "SELECT ",
        columns!(),
        " FROM linch_worker_schedules WHERE enabled AND next_fire <= now() \
         ORDER BY next_fire LIMIT $1 FOR UPDATE SKIP LOCKED"
    ))
    .bind(FIRE_BATCH)
    .fetch_all(&mut *tx)
    .await?;
    let mut fired = 0;
    for row in due {
        let schedule = info(row);
        if fire(&mut tx, &schedule).await? {
            fired += 1;
        }
    }
    tx.commit().await?;
    Ok(fired)
}

/// Make `schedule`'s due fire inside `tx`; whether it enqueued a job.
async fn fire(
    tx: &mut Transaction<'static, Postgres>,
    schedule: &ScheduleInfo,
) -> Result<bool, WorkerError> {
    let (namespace, id) = (&schedule.namespace, &schedule.id);
    let fire_at = schedule.next_fire;
    let next = match schedule.timing.next_after(fire_at) {
        Ok(next) => next,
        Err(error) => {
            tracing::warn!(%namespace, %id, error = chain(&error), "pausing a schedule");
            sqlx::query(
                "UPDATE linch_worker_schedules SET enabled = FALSE \
                 WHERE namespace = $1 AND id = $2",
            )
            .bind(namespace)
            .bind(id)
            .execute(&mut **tx)
            .await?;
            return Ok(false);
        }
    };
    let stamp = fire_at.to_rfc3339_opts(SecondsFormat::Micros, true);
    let key = format!("schedule:{id}:{stamp}");
    let busy = match &schedule.session_id {
        Some(session) => session_busy(tx, schedule, session).await?,
        None => false,
    };
    let enqueued = if busy {
        tracing::info!(%namespace, %id, %stamp, "skipping a fire: its session is busy");
        false
    } else {
        let session = match &schedule.session_id {
            Some(session) => session.as_str().to_owned(),
            None => key.clone(),
        };
        let job = NewJob {
            queue: &schedule.queue,
            namespace,
            session_id: Some(&session),
            prompt: Some(&schedule.prompt),
            dedupe_key: Some(key),
            agent: schedule.agent.as_deref(),
            schedule_id: Some(id),
            fire_at: Some(fire_at),
            ..NewJob::default()
        };
        matches!(insert(tx, job).await?, Inserted::New(_))
    };
    sqlx::query(
        "UPDATE linch_worker_schedules SET next_fire = $3, last_fire = $4, \
         fires = fires + $5, skips = skips + $6 WHERE namespace = $1 AND id = $2",
    )
    .bind(namespace)
    .bind(id)
    .bind(next)
    .bind(fire_at)
    .bind(i64::from(enqueued))
    .bind(i64::from(busy))
    .execute(&mut **tx)
    .await?;
    Ok(enqueued)
}

/// Whether a fire of `schedule` into its fixed `session` must be skipped:
/// an earlier fire's job is unfinished, or the session has a run that is
/// not terminal (one in progress, or one suspended for an approval or an
/// answer, which only a person ends).
async fn session_busy(
    tx: &mut Transaction<'static, Postgres>,
    schedule: &ScheduleInfo,
    session: &SessionId,
) -> Result<bool, WorkerError> {
    let busy: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM linch_worker_jobs \
                        WHERE namespace = $1 AND schedule_id = $2 AND failed_at IS NULL) \
             OR EXISTS (SELECT 1 FROM linch_runs \
                        WHERE namespace = $1 AND session_id = $3 \
                          AND status NOT IN ('completed', 'failed', 'aborted'))",
    )
    .bind(&schedule.namespace)
    .bind(&schedule.id)
    .bind(session.as_str())
    .fetch_one(&mut **tx)
    .await?;
    Ok(busy)
}
