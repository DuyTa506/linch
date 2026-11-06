//! Cron schedules (UTC) that enqueue new runs.
//!
//! [`fire_due_schedules`] locks the due rows with `FOR UPDATE SKIP LOCKED`,
//! enqueues one start job per row and moves `next_fire` forward, all in one
//! transaction. A fire therefore happens once however many workers poll: a
//! second poller skips the locked rows, and after the commit the rows are no
//! longer due. Each job also carries the dedupe key
//! `schedule:{id}:{fire time}`, a second guard against a double fire.
//!
//! Missed fires are not replayed: a schedule that is overdue by several
//! ticks (every worker was down) fires once, and its next fire is the first
//! tick after the database's current time.

use std::str::FromStr;

use chrono::{DateTime, SecondsFormat, Utc};
use croner::Cron;
use linch_core::SessionId;
use linch_postgres::PgPool;

use crate::error::{WorkerError, chain, non_blank};
use crate::job::{Inserted, NewJob, insert};

/// Most due schedules one [`fire_due_schedules`] call fires.
const FIRE_BATCH: i64 = 64;

/// A cron schedule that enqueues a run of `prompt` in `session_id` at every
/// fire.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Schedule {
    /// Store namespace of the agent that runs the fires; schedule ids are
    /// unique within it.
    pub namespace: String,
    /// Schedule id.
    pub id: String,
    /// Queue the fires go to.
    pub queue: String,
    /// Session every fire's run belongs to.
    pub session_id: SessionId,
    /// The user prompt of every fire's run.
    pub prompt: String,
    /// Cron pattern in UTC: five fields, or six with a leading seconds
    /// field (croner syntax).
    pub cron: String,
}

fn parse(pattern: &str) -> Result<Cron, WorkerError> {
    Cron::from_str(pattern).map_err(|source| WorkerError::Cron {
        pattern: pattern.to_owned(),
        source,
    })
}

/// The first fire of `cron` strictly after `after`.
fn next_fire(
    cron: &Cron,
    pattern: &str,
    after: DateTime<Utc>,
) -> Result<DateTime<Utc>, WorkerError> {
    cron.find_next_occurrence(&after, false)
        .map_err(|source| WorkerError::Cron {
            pattern: pattern.to_owned(),
            source,
        })
}

/// Store `schedule`, its first fire being the first cron tick after the
/// database's current time.
///
/// Args:
///     pool: Pool for the queue's database.
///     schedule: The schedule; its `(namespace, id)` must be new.
///
/// Returns:
///     The time of the first fire.
///
/// Raises:
///     WorkerError: If a name is blank, the cron pattern is invalid or
///         never fires, or the insert fails (also when the id exists).
pub async fn create_schedule(
    pool: &PgPool,
    schedule: &Schedule,
) -> Result<DateTime<Utc>, WorkerError> {
    non_blank(&schedule.namespace, "the namespace")?;
    non_blank(&schedule.id, "the schedule id")?;
    non_blank(&schedule.queue, "the queue")?;
    let cron = parse(&schedule.cron)?;
    let mut tx = crate::begin(pool).await?;
    let now: DateTime<Utc> = sqlx::query_scalar("SELECT now()")
        .fetch_one(&mut *tx)
        .await?;
    let first = next_fire(&cron, &schedule.cron, now)?;
    sqlx::query(
        "INSERT INTO linch_worker_schedules \
         (namespace, id, queue, session_id, prompt, cron, next_fire) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&schedule.namespace)
    .bind(&schedule.id)
    .bind(&schedule.queue)
    .bind(schedule.session_id.as_str())
    .bind(&schedule.prompt)
    .bind(&schedule.cron)
    .bind(first)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(first)
}

/// Stop a schedule's future fires. Runs it already enqueued stay queued.
///
/// Args:
///     pool: Pool for the queue's database.
///     namespace: The schedule's namespace.
///     id: The schedule id.
///
/// Returns:
///     Whether an enabled schedule was disabled.
///
/// Raises:
///     WorkerError: If the update fails.
pub async fn cancel_schedule(
    pool: &PgPool,
    namespace: &str,
    id: &str,
) -> Result<bool, WorkerError> {
    let done = sqlx::query(
        "UPDATE linch_worker_schedules SET enabled = FALSE \
         WHERE namespace = $1 AND id = $2 AND enabled",
    )
    .bind(namespace)
    .bind(id)
    .execute(pool)
    .await?;
    Ok(done.rows_affected() == 1)
}

type DueRow = (
    String,
    String,
    String,
    String,
    String,
    String,
    DateTime<Utc>,
    DateTime<Utc>,
);

/// Fire every due schedule once: enqueue its start job and move its next
/// fire past the database's current time. Workers call this on every poll;
/// a host may call it too.
///
/// A stored pattern that no longer parses disables its schedule (logged)
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
    let due: Vec<DueRow> = sqlx::query_as(
        "SELECT namespace, id, queue, session_id, prompt, cron, next_fire, now() \
         FROM linch_worker_schedules WHERE enabled AND next_fire <= now() \
         ORDER BY next_fire LIMIT $1 FOR UPDATE SKIP LOCKED",
    )
    .bind(FIRE_BATCH)
    .fetch_all(&mut *tx)
    .await?;
    let mut fired = 0;
    for (namespace, id, queue, session_id, prompt, pattern, fire, now) in due {
        let next = parse(&pattern).and_then(|cron| next_fire(&cron, &pattern, fire.max(now)));
        let next = match next {
            Ok(next) => next,
            Err(error) => {
                tracing::warn!(%namespace, %id, error = chain(&error), "disabling a schedule");
                sqlx::query(
                    "UPDATE linch_worker_schedules SET enabled = FALSE \
                     WHERE namespace = $1 AND id = $2",
                )
                .bind(&namespace)
                .bind(&id)
                .execute(&mut *tx)
                .await?;
                continue;
            }
        };
        let key = format!(
            "schedule:{id}:{}",
            fire.to_rfc3339_opts(SecondsFormat::Micros, true)
        );
        let job = NewJob {
            queue: &queue,
            namespace: &namespace,
            session_id: Some(&session_id),
            prompt: Some(&prompt),
            dedupe_key: Some(key),
            ..NewJob::default()
        };
        if let Inserted::New(_) = insert(&mut tx, job).await? {
            fired += 1;
        }
        sqlx::query(
            "UPDATE linch_worker_schedules SET next_fire = $3 WHERE namespace = $1 AND id = $2",
        )
        .bind(&namespace)
        .bind(&id)
        .bind(next)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(fired)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(text: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(text).unwrap().to_utc()
    }

    #[test]
    fn the_next_fire_is_strictly_after() {
        let hourly = parse("0 * * * *").unwrap();
        let next = next_fire(&hourly, "0 * * * *", at("2026-11-06T10:15:00Z")).unwrap();
        assert_eq!(next, at("2026-11-06T11:00:00Z"));
        // A fire exactly on a tick moves to the following tick.
        let next = next_fire(&hourly, "0 * * * *", at("2026-11-06T11:00:00Z")).unwrap();
        assert_eq!(next, at("2026-11-06T12:00:00Z"));
    }

    #[test]
    fn a_seconds_field_is_accepted() {
        let every_ten = parse("*/10 * * * * *").unwrap();
        let next = next_fire(&every_ten, "", at("2026-11-06T10:15:01Z")).unwrap();
        assert_eq!(next, at("2026-11-06T10:15:10Z"));
    }

    #[test]
    fn a_bad_pattern_names_itself() {
        let error = parse("61 * * * *").unwrap_err();
        assert!(matches!(&error, WorkerError::Cron { pattern, .. } if pattern == "61 * * * *"));
        assert!(parse("not a cron").is_err());
    }
}
