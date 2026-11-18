//! Retention: delete what finished runs and idle sessions left behind, in
//! small batches, for a host that has to bound its tables.
//!
//! Off unless configured. A pass deletes in two independent steps, each
//! optional: runs that ended more than `terminal_runs` ago (the run row and
//! its events; a small tombstone says the run was pruned), and sessions
//! that nothing wrote to for `idle_sessions` (the log, the tasks and their
//! edges). Run events are never deleted without their run, and a session is
//! never deleted while it has a run that is not terminal.

use std::time::Duration;

use chrono::{DateTime, Utc};
use linch_postgres::PgPool;
use sqlx::{Postgres, Transaction};
use tokio::time::Instant;

use crate::error::WorkerError;

/// How long a pruned run is remembered by default: 30 days.
pub const TOMBSTONE_PERIOD: Duration = Duration::from_secs(30 * 24 * 3600);

/// The most runs or sessions one batch deletes, by default.
pub const BATCH_SIZE: u32 = 500;

/// The most a batch may be, so that its transaction stays short.
pub const MAX_BATCH_SIZE: u32 = 10_000;

/// Least time between two passes, by default: an hour.
pub const PASS_EVERY: Duration = Duration::from_secs(3600);

/// The least `interval`: a pass is a scan of the tables.
pub const MIN_PASS_EVERY: Duration = Duration::from_secs(60);

/// What to keep, and how to delete the rest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retention {
    /// Delete a terminal run (and its events) that ended longer ago than
    /// this; `None` keeps every run.
    pub terminal_runs: Option<Duration>,
    /// Delete a session (its log and tasks) that nothing wrote to for this
    /// long and that has no run that is not terminal; `None` keeps every
    /// session.
    pub idle_sessions: Option<Duration>,
    /// How long a deleted run is remembered, so that a read of it says it
    /// was pruned and not that it never existed; [`TOMBSTONE_PERIOD`].
    pub tombstones: Duration,
    /// The most runs or sessions one transaction deletes; [`BATCH_SIZE`].
    pub batch_size: u32,
    /// Least time between two passes; [`PASS_EVERY`].
    pub interval: Duration,
}

impl Default for Retention {
    fn default() -> Self {
        Self {
            terminal_runs: None,
            idle_sessions: None,
            tombstones: TOMBSTONE_PERIOD,
            batch_size: BATCH_SIZE,
            interval: PASS_EVERY,
        }
    }
}

impl Retention {
    /// Refuse a configuration that deletes nothing or cannot work.
    ///
    /// Raises:
    ///     WorkerError: `Invalid` if neither window is set, a window or the
    ///         tombstone period is under a minute, the batch size is 0 or
    ///         over [`MAX_BATCH_SIZE`], or the interval is under
    ///         [`MIN_PASS_EVERY`].
    pub fn check(&self) -> Result<(), WorkerError> {
        let invalid = |text: String| Err(WorkerError::Invalid(text));
        if self.terminal_runs.is_none() && self.idle_sessions.is_none() {
            return invalid("retention needs terminal_runs or idle_sessions".into());
        }
        let minute = Duration::from_secs(60);
        for (name, value) in [
            ("terminal_runs", self.terminal_runs),
            ("idle_sessions", self.idle_sessions),
            ("tombstones", Some(self.tombstones)),
        ] {
            if value.is_some_and(|value| value < minute) {
                return invalid(format!("retention {name} is under a minute"));
            }
        }
        if self.batch_size == 0 || self.batch_size > MAX_BATCH_SIZE {
            return invalid(format!(
                "retention batch_size must be 1 to {MAX_BATCH_SIZE}, not {}",
                self.batch_size
            ));
        }
        if self.interval < MIN_PASS_EVERY {
            return invalid(format!(
                "retention interval must be at least {} s",
                MIN_PASS_EVERY.as_secs()
            ));
        }
        Ok(())
    }
}

/// What one pass deleted.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Pruned {
    /// Runs that were terminal before the retention migration, which the
    /// pass gave an end time (now) so that their window starts.
    pub stamped: u64,
    /// Run rows.
    pub runs: u64,
    /// Run events.
    pub events: u64,
    /// Sessions.
    pub sessions: u64,
    /// Session log entries.
    pub entries: u64,
    /// Tombstones that outlived their period.
    pub tombstones: u64,
    /// Whether another pruner held the lock, so the pass stopped early.
    pub contended: bool,
}

/// One pruner at a time per database and schema: a transaction-scoped
/// try-lock, taken by every batch, so it needs no connection of its own and
/// dies with a crashed pruner.
const LOCK: &str = "SELECT pg_try_advisory_xact_lock(hashtextextended(\
    'linch_retention:' || current_database() || ':' || coalesce(current_schema(), ''), 0))";

/// Give the runs that were terminal before migration 10 an end time of now,
/// a batch at a time: the migration rewrites nothing, so their window starts
/// at the first pass that has one. A run that becomes terminal afterwards is
/// stamped by its own commit and is never in this set.
const STAMP: &str = "UPDATE linch_runs SET ended_at = now() \
    WHERE (namespace, run_id) IN (SELECT namespace, run_id FROM linch_runs \
        WHERE ended_at IS NULL AND status IN ('completed', 'failed', 'aborted') \
        ORDER BY namespace, run_id LIMIT $1 FOR UPDATE SKIP LOCKED)";

/// The runs to delete next, in key order after the cursor `($2, $3, $4)`:
/// terminal, ended before the cutoff, with no live parent (a parent reads
/// its children's events when it resumes), no job that still names them (a
/// retried start job would start another run), and not a workflow's agent
/// call (`wf-`: a resumed workflow reads its events when its journal has no
/// record yet). Each row stays locked until the batch ends.
const PICK_RUNS: &str = "SELECT r.ended_at, r.namespace, r.run_id FROM linch_runs AS r \
    WHERE r.ended_at IS NOT NULL AND r.ended_at < now() - $1 * interval '1 millisecond' \
      AND (r.ended_at, r.namespace, r.run_id) > ($2, $3, $4) \
      AND r.status IN ('completed', 'failed', 'aborted') \
      AND r.run_id NOT LIKE 'wf-%' \
      AND (r.parent_run_id IS NULL OR NOT EXISTS ( \
          SELECT 1 FROM linch_runs AS p \
          WHERE p.namespace = r.namespace AND p.run_id = r.parent_run_id \
            AND p.status NOT IN ('completed', 'failed', 'aborted'))) \
      AND NOT EXISTS (SELECT 1 FROM linch_worker_jobs AS j \
                      WHERE j.namespace = r.namespace AND j.run_id = r.run_id \
                        AND j.failed_at IS NULL) \
    ORDER BY r.ended_at, r.namespace, r.run_id LIMIT $5 FOR UPDATE OF r SKIP LOCKED";

/// Delete the picked runs, their events and write their tombstones in one
/// statement; the delete re-checks the status. The foreign key is checked
/// at the end of the statement, when the events are gone too.
const DELETE_RUNS: &str = "WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(ns, id)), \
    gone AS (DELETE FROM linch_runs AS r USING k \
             WHERE r.namespace = k.ns AND r.run_id = k.id \
               AND r.status IN ('completed', 'failed', 'aborted') \
             RETURNING r.namespace, r.run_id), \
    ev AS (DELETE FROM linch_run_events AS e USING gone AS g \
           WHERE e.namespace = g.namespace AND e.run_id = g.run_id RETURNING 1), \
    tomb AS (INSERT INTO linch_pruned_runs (namespace, run_id) \
             SELECT namespace, run_id FROM gone ON CONFLICT DO NOTHING RETURNING 1) \
    SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM ev)";

/// The sessions to delete next, in key order after the cursor `($2, $3)`:
/// idle, with no run that is not terminal, and not a workflow's.
const PICK_SESSIONS: &str = "SELECT s.namespace, s.session_id FROM linch_sessions AS s \
    WHERE (s.namespace, s.session_id) > ($2, $3) \
      AND s.updated_at < now() - $1 * interval '1 millisecond' \
      AND s.session_id NOT LIKE 'wf-%' \
      AND NOT EXISTS (SELECT 1 FROM linch_runs AS r \
                      WHERE r.namespace = s.namespace AND r.session_id = s.session_id \
                        AND r.status NOT IN ('completed', 'failed', 'aborted')) \
    ORDER BY s.namespace, s.session_id LIMIT $4 FOR UPDATE OF s SKIP LOCKED";

/// Delete the picked sessions and what hangs off them in one statement;
/// the delete re-checks that the session is idle and has no live run (a
/// run created after the pick commits a row this statement sees).
const DELETE_SESSIONS: &str = "WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(ns, id)), \
    gone AS (DELETE FROM linch_sessions AS s USING k \
             WHERE s.namespace = k.ns AND s.session_id = k.id \
               AND s.updated_at < now() - $3 * interval '1 millisecond' \
               AND NOT EXISTS (SELECT 1 FROM linch_runs AS r \
                               WHERE r.namespace = s.namespace AND r.session_id = s.session_id \
                                 AND r.status NOT IN ('completed', 'failed', 'aborted')) \
             RETURNING s.namespace, s.session_id), \
    en AS (DELETE FROM linch_session_entries AS e USING gone AS g \
           WHERE e.namespace = g.namespace AND e.session_id = g.session_id RETURNING 1), \
    ed AS (DELETE FROM linch_task_edges AS e USING gone AS g \
           WHERE e.namespace = g.namespace AND e.session_id = g.session_id RETURNING 1), \
    tk AS (DELETE FROM linch_tasks AS t USING gone AS g \
           WHERE t.namespace = g.namespace AND t.session_id = g.session_id RETURNING 1) \
    SELECT (SELECT count(*) FROM gone), (SELECT count(*) FROM en)";

/// How a batch ended.
enum Batch<K> {
    /// Another pruner holds the lock.
    Contended,
    /// The batch's rows were deleted: the last key picked (the next
    /// batch's cursor), and what the delete counted.
    Done(Option<K>, i64, i64),
}

fn millis(period: Duration) -> f64 {
    period.as_millis() as f64
}

fn count(n: i64) -> u64 {
    u64::try_from(n).unwrap_or(0)
}

/// Take the pruner lock in `tx`; whether it is ours.
async fn lock(tx: &mut Transaction<'static, Postgres>) -> Result<bool, WorkerError> {
    Ok(sqlx::query_scalar(LOCK).fetch_one(&mut **tx).await?)
}

/// One batch of terminal runs older than `window` after `cursor`.
async fn runs_batch(
    pool: &PgPool,
    window: Duration,
    batch: u32,
    cursor: &(DateTime<Utc>, String, String),
) -> Result<Batch<(DateTime<Utc>, String, String)>, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    if !lock(&mut tx).await? {
        return Ok(Batch::Contended);
    }
    let picked: Vec<(DateTime<Utc>, String, String)> = sqlx::query_as(PICK_RUNS)
        .bind(millis(window))
        .bind(cursor.0)
        .bind(&cursor.1)
        .bind(&cursor.2)
        .bind(i64::from(batch))
        .fetch_all(&mut *tx)
        .await?;
    let Some(last) = picked.last().cloned() else {
        tx.commit().await?;
        return Ok(Batch::Done(None, 0, 0));
    };
    let (namespaces, ids): (Vec<String>, Vec<String>) = picked
        .into_iter()
        .map(|(_, namespace, run_id)| (namespace, run_id))
        .unzip();
    let (runs, events): (i64, i64) = sqlx::query_as(DELETE_RUNS)
        .bind(namespaces)
        .bind(ids)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Batch::Done(Some(last), runs, events))
}

/// One batch of sessions idle longer than `window` after `cursor`.
async fn sessions_batch(
    pool: &PgPool,
    window: Duration,
    batch: u32,
    cursor: &(String, String),
) -> Result<Batch<(String, String)>, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    if !lock(&mut tx).await? {
        return Ok(Batch::Contended);
    }
    let picked: Vec<(String, String)> = sqlx::query_as(PICK_SESSIONS)
        .bind(millis(window))
        .bind(&cursor.0)
        .bind(&cursor.1)
        .bind(i64::from(batch))
        .fetch_all(&mut *tx)
        .await?;
    let Some(last) = picked.last().cloned() else {
        tx.commit().await?;
        return Ok(Batch::Done(None, 0, 0));
    };
    let (namespaces, ids): (Vec<String>, Vec<String>) = picked.into_iter().unzip();
    let (sessions, entries): (i64, i64) = sqlx::query_as(DELETE_SESSIONS)
        .bind(namespaces)
        .bind(ids)
        .bind(millis(window))
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Batch::Done(Some(last), sessions, entries))
}

/// Stamp one batch of runs that were terminal before the migration; how
/// many, or `None` when another pruner holds the lock.
async fn stamp(pool: &PgPool, batch: u32) -> Result<Option<u64>, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    if !lock(&mut tx).await? {
        return Ok(None);
    }
    let stamped = sqlx::query(STAMP)
        .bind(i64::from(batch))
        .execute(&mut *tx)
        .await?
        .rows_affected();
    tx.commit().await?;
    Ok(Some(stamped))
}

/// Delete the tombstones older than `period`, a batch at a time.
async fn tombstones(
    pool: &PgPool,
    period: Duration,
    batch: u32,
) -> Result<Option<u64>, WorkerError> {
    let mut tx = crate::begin(pool).await?;
    if !lock(&mut tx).await? {
        return Ok(None);
    }
    let deleted = sqlx::query(
        "DELETE FROM linch_pruned_runs WHERE (namespace, run_id) IN ( \
             SELECT namespace, run_id FROM linch_pruned_runs \
             WHERE pruned_at < now() - $1 * interval '1 millisecond' \
             ORDER BY pruned_at LIMIT $2)",
    )
    .bind(millis(period))
    .bind(i64::from(batch))
    .execute(&mut *tx)
    .await?
    .rows_affected();
    tx.commit().await?;
    Ok(Some(deleted))
}

/// Pause as long as the batch took, so a pass keeps the database at most
/// half busy: the batches are plain deletes, and what they write (WAL, dirty
/// pages, index changes) is shared with every run that is committing.
async fn rest(started: Instant) {
    tokio::time::sleep(started.elapsed()).await;
}

/// Run one retention pass: delete what `retention` says in batches, each its
/// own transaction (a crash leaves every run and session either whole or
/// gone, with its tombstone), until nothing older than the windows is left.
///
/// Each batch is followed by a pause as long as the batch took, so a pass
/// leaves the database at least half free for the runs that are committing.
/// A pass walks each table once in key order, so a row it could not delete
/// (a child of a live run, say) is not read again until the next pass. A
/// pass stops early, with `contended` set, when another pruner (another
/// server's worker) holds the database's pruner lock.
///
/// Args:
///     pool: Pool for the database and schema of the stores.
///     retention: What to keep; checked first.
///
/// Returns:
///     What the pass deleted.
///
/// Raises:
///     WorkerError: `Invalid` for a configuration that [`Retention::check`]
///         refuses, or `Database` if a statement fails; what earlier batches
///         deleted stays deleted.
pub async fn prune_retained(pool: &PgPool, retention: &Retention) -> Result<Pruned, WorkerError> {
    retention.check()?;
    let mut done = Pruned::default();
    if let Some(window) = retention.terminal_runs {
        loop {
            let started = Instant::now();
            match stamp(pool, retention.batch_size).await? {
                None => {
                    done.contended = true;
                    return Ok(done);
                }
                Some(0) => break,
                Some(n) => done.stamped += n,
            }
            rest(started).await;
        }
        let mut cursor = (DateTime::<Utc>::UNIX_EPOCH, String::new(), String::new());
        loop {
            let started = Instant::now();
            match runs_batch(pool, window, retention.batch_size, &cursor).await? {
                Batch::Contended => {
                    done.contended = true;
                    return Ok(done);
                }
                Batch::Done(None, ..) => break,
                Batch::Done(Some(last), runs, events) => {
                    done.runs += count(runs);
                    done.events += count(events);
                    cursor = last;
                }
            }
            rest(started).await;
        }
    }
    if let Some(window) = retention.idle_sessions {
        let mut cursor = (String::new(), String::new());
        loop {
            let started = Instant::now();
            match sessions_batch(pool, window, retention.batch_size, &cursor).await? {
                Batch::Contended => {
                    done.contended = true;
                    return Ok(done);
                }
                Batch::Done(None, ..) => break,
                Batch::Done(Some(last), sessions, entries) => {
                    done.sessions += count(sessions);
                    done.entries += count(entries);
                    cursor = last;
                }
            }
            rest(started).await;
        }
    }
    loop {
        let started = Instant::now();
        match tombstones(pool, retention.tombstones, retention.batch_size).await? {
            None => {
                done.contended = true;
                break;
            }
            Some(0) => break,
            Some(n) => done.tombstones += n,
        }
        rest(started).await;
    }
    Ok(done)
}

#[cfg(test)]
mod tests;
