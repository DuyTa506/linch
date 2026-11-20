//! [`PgRunStore`]: runs, checkpoints and the event journal in Postgres.
//!
//! A commit is one statement in autocommit: it moves the run row only when
//! the lease epoch, the status and the last seq allow it, and inserts the
//! events from the moved row. A refused commit reads the row once more and
//! names the refusal in linch-core's order: not found, lease epoch, status,
//! event run ids, then seqs. `create` and `claim` are transactions that
//! lock the run row first. A rejected or interrupted write changes nothing.
//!
//! Each store instance is one lease holder. A claim records it and a lease
//! [`DEFAULT_RUN_LEASE_TTL`] long on the database clock; a claim through
//! another instance is refused while the run is `running` under a lease
//! that has not run out. Renewals and releases are single statements that
//! match the lease's epoch. Commits are fenced by the epoch alone.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch_core::{
    Commit, Envelope, Lease, NewRun, RunId, RunRecord, RunStatus, RunStore, StoreError,
};
use sqlx::{PgExecutor, PgPool, Postgres, Transaction};

use crate::codec::{
    backend, check_events, decode_checkpoint, decode_event, decode_principal, encode_checkpoint,
    encode_events, encode_principal, not_found, parse_contract, parse_status, status_name, to_i64,
    to_u64,
};
use crate::lease_line::LeaseLine;

/// How long a run claim holds without a renewal, unless set with
/// [`PgRunStore::with_lease_ttl`]. A run's driver renews it every third
/// (10 s); a holder that stops (a crash, a frozen process) keeps other
/// processes from its run for at most this long. It matches the worker's
/// default visibility timeout, so a job taken over after its worker went
/// quiet finds the run free at about the same time.
pub const DEFAULT_RUN_LEASE_TTL: Duration = Duration::from_secs(30);

/// A [`RunStore`] backed by Postgres.
///
/// Records are keyed by `(namespace, run_id)`. Run [`crate::migrate`] on the
/// database before the first call.
///
/// Each store made with [`PgRunStore::new`] is its own lease holder, and its
/// clones share it: runs claimed through it are refused to every other
/// store while their leases live. Build one per agent or per process.
#[derive(Debug, Clone)]
pub struct PgRunStore {
    pool: PgPool,
    /// This store's identity in `lease_holder`.
    holder: String,
    lease_ttl: Duration,
    /// Renewals and releases, which never queue behind run work in `pool`.
    line: Arc<LeaseLine>,
}

impl PgRunStore {
    /// A store that runs its queries on `pool`: a new lease holder, with
    /// leases [`DEFAULT_RUN_LEASE_TTL`] long.
    ///
    /// Renewing and releasing leases use one connection of the store's own,
    /// opened outside `pool` on the first renewal, so a pool whose
    /// connections are all busy (or waiting on a row lock) does not delay a
    /// beat: budget one connection per store beyond `pool`'s maximum. Each
    /// of those writes gives up after a third of the lease.
    pub fn new(pool: PgPool) -> Self {
        Self {
            line: Arc::new(LeaseLine::new(&pool, DEFAULT_RUN_LEASE_TTL / 3)),
            pool,
            holder: uuid::Uuid::new_v4().to_string(),
            lease_ttl: DEFAULT_RUN_LEASE_TTL,
        }
    }

    /// The same store with leases `ttl` long: the longest another process
    /// waits for a run whose holder stopped, against one renewal per
    /// running run every third of it.
    ///
    /// Raises:
    ///     Panics: If `ttl` is shorter than a millisecond.
    pub fn with_lease_ttl(mut self, ttl: Duration) -> Self {
        assert!(
            ttl >= Duration::from_millis(1),
            "a run lease lasts at least 1 ms"
        );
        self.lease_ttl = ttl;
        self.line = Arc::new(LeaseLine::new(&self.pool, ttl / 3));
        self
    }

    /// The lease length in milliseconds, for SQL.
    fn ttl_ms(&self) -> f64 {
        self.lease_ttl.as_secs_f64() * 1000.0
    }
}

type RecordRow = (
    String,
    Option<String>,
    Option<String>,
    String,
    String,
    i64,
    String,
    i64,
);

const STATE: &str = "SELECT status, epoch, last_seq FROM linch_runs \
                     WHERE namespace = $1 AND run_id = $2";

/// A claim's read, with the row locked: the run's status and epoch, its
/// lease's owner and holder, and the whole milliseconds its lease has left
/// (0 for none: `GREATEST` skips a NULL).
const CLAIM_LOCK: &str = "SELECT status, epoch, owner, lease_holder, \
                          GREATEST(0, ceil(extract(epoch FROM lease_until - clock_timestamp()) \
                          * 1000))::bigint \
                          FROM linch_runs WHERE namespace = $1 AND run_id = $2 FOR UPDATE";

type ClaimRow = (String, i64, Option<String>, Option<String>, i64);

/// A claim's write: status `running`, the new epoch, and a fresh lease.
const CLAIM: &str = "UPDATE linch_runs SET status = $3, epoch = $4, owner = $5, \
                     lease_holder = $6, \
                     lease_until = clock_timestamp() + $7::float8 * interval '1 millisecond' \
                     WHERE namespace = $1 AND run_id = $2";

/// A renewal: a whole TTL from now, only under the lease's epoch, and only
/// while the lease has not been released (a release sets `lease_until` to
/// NULL), so a renewal that lands after the release does not lease the run
/// again.
const RENEW: &str = "UPDATE linch_runs \
                     SET lease_until = clock_timestamp() + $4::float8 * interval '1 millisecond' \
                     WHERE namespace = $1 AND run_id = $2 AND epoch = $3 \
                       AND lease_until IS NOT NULL";

/// A release: no live lease, only under the lease's epoch.
const RELEASE: &str = "UPDATE linch_runs SET lease_until = NULL \
                       WHERE namespace = $1 AND run_id = $2 AND epoch = $3";

/// Status, epoch and last seq of a run row, read with [`STATE`].
async fn run_state<'e>(
    db: impl PgExecutor<'e>,
    namespace: &str,
    run_id: &RunId,
) -> Result<(RunStatus, u64, u64), StoreError> {
    let row: Option<(String, i64, i64)> = sqlx::query_as(STATE)
        .bind(namespace)
        .bind(run_id.as_str())
        .fetch_optional(db)
        .await
        .map_err(backend)?;
    let (status, epoch, last_seq) =
        row.ok_or_else(|| not_found("run", namespace, run_id.as_str()))?;
    Ok((
        parse_status(&status)?,
        to_u64(epoch, "epoch")?,
        to_u64(last_seq, "last_seq")?,
    ))
}

/// A commit in one statement. The run row moves only under the lease's
/// epoch, while `running`, and, for a commit with events, when its last seq
/// is `$6`, the one the events continue. The events are inserted from the
/// moved row, so they land with it or not at all.
const COMMIT: &str = "WITH moved AS ( \
         UPDATE linch_runs SET status = COALESCE($4, status), \
         checkpoint = COALESCE($5, checkpoint), \
         last_seq = last_seq + cardinality($7::bigint[]), \
         ended_at = CASE WHEN COALESCE($4, status) IN ('completed', 'failed', 'aborted') \
                         THEN now() ELSE ended_at END \
         WHERE namespace = $1 AND run_id = $2 AND epoch = $3 AND status = 'running' \
         AND ($6::bigint IS NULL OR last_seq = $6) RETURNING 1 \
     ), inserted AS ( \
         INSERT INTO linch_run_events (namespace, run_id, seq, envelope) \
         SELECT $1, $2, seq, envelope \
         FROM moved, unnest($7::bigint[], $8::text[]) AS e(seq, envelope) \
     ) SELECT count(*) FROM moved";

/// Why a run with this status, epoch and last seq refuses `commit` under
/// `lease`, in linch-core's order; `Ok` when it would take it.
fn refusal(
    lease: &Lease,
    commit: &Commit,
    (from, epoch, last_seq): (RunStatus, u64, u64),
) -> Result<(), StoreError> {
    if lease.epoch != epoch {
        return Err(StoreError::LeaseLost {
            run_id: lease.run_id.clone(),
            epoch: lease.epoch,
        });
    }
    let to = commit.status.unwrap_or(from);
    if from != RunStatus::Running || to == RunStatus::Pending {
        return Err(StoreError::InvalidTransition { from, to });
    }
    check_events(&lease.run_id, last_seq, &commit.events)
}

async fn insert_events(
    tx: &mut Transaction<'static, Postgres>,
    namespace: &str,
    run_id: &RunId,
    events: (Vec<i64>, Vec<String>),
) -> Result<(), StoreError> {
    let (seqs, envelopes) = events;
    if seqs.is_empty() {
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO linch_run_events (namespace, run_id, seq, envelope) \
         SELECT $1, $2, seq, envelope FROM unnest($3::bigint[], $4::text[]) AS e(seq, envelope)",
    )
    .bind(namespace)
    .bind(run_id.as_str())
    .bind(seqs)
    .bind(envelopes)
    .execute(&mut **tx)
    .await
    .map_err(backend)?;
    Ok(())
}

/// The unique index of migration 2: one run that is not terminal per
/// session and namespace.
const LIVE_PER_SESSION: &str = "linch_runs_one_live_per_session";

/// The live run of a session; the predicate is the index's, so the index
/// serves the read.
const LIVE_RUN: &str = "SELECT run_id FROM linch_runs WHERE namespace = $1 AND session_id = $2 \
                        AND status NOT IN ('completed', 'failed', 'aborted')";

/// Most inserts one `create` tries when the run it lost to ends before its
/// id is read.
const ADMIT_TRIES: u32 = 3;

/// How one insert attempt of a `create` ended.
enum Insert {
    Done,
    /// The session's live-run index refused the row; nothing was written.
    Busy(sqlx::Error),
}

fn session_busy(error: &sqlx::Error) -> bool {
    error.as_database_error().is_some_and(|database| {
        database.is_unique_violation() && database.constraint() == Some(LIVE_PER_SESSION)
    })
}

impl PgRunStore {
    /// The error for a run with no row: `NotFound`, `pruned` when a
    /// retention pass deleted it. Only this not-found path reads the
    /// tombstones, so no read of a run that exists pays for them. A failed
    /// tombstone read is the error, and not a guess.
    async fn missing(&self, namespace: &str, run_id: &RunId) -> StoreError {
        let pruned = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM linch_pruned_runs WHERE namespace = $1 AND run_id = $2)",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .fetch_one(&self.pool)
        .await;
        match pruned {
            Ok(true) => StoreError::NotFound {
                kind: "run",
                namespace: namespace.to_owned(),
                id: run_id.as_str().to_owned(),
                pruned: true,
            },
            Ok(false) => not_found("run", namespace, run_id.as_str()),
            Err(error) => backend(error),
        }
    }

    /// Run [`COMMIT`]; whether the run row took it.
    async fn write(
        &self,
        lease: &Lease,
        commit: &Commit,
        checkpoint: Option<&str>,
        (seqs, envelopes): &(Vec<i64>, Vec<String>),
    ) -> Result<bool, sqlx::Error> {
        // No row holds an epoch past i64::MAX; the refusal read names it.
        let Ok(epoch) = i64::try_from(lease.epoch) else {
            return Ok(false);
        };
        let moved: i64 = sqlx::query_scalar(COMMIT)
            .bind(&lease.namespace)
            .bind(lease.run_id.as_str())
            .bind(epoch)
            .bind(commit.status.map(status_name))
            .bind(checkpoint)
            .bind(seqs.first().map(|seq| seq - 1))
            .bind(seqs.as_slice())
            .bind(envelopes.as_slice())
            .fetch_one(&self.pool)
            .await?;
        Ok(moved == 1)
    }

    /// Claim `run_id`; over another holder's live lease only when `force`.
    async fn take(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
        force: bool,
    ) -> Result<Lease, StoreError> {
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        let row: Option<ClaimRow> = sqlx::query_as(CLAIM_LOCK)
            .bind(namespace)
            .bind(run_id.as_str())
            .fetch_optional(&mut *tx)
            .await
            .map_err(backend)?;
        let Some((status, epoch, held_by, holder, left_ms)) = row else {
            // Back to the pool first: `missing` reads on a connection of its
            // own, and a pool of one has no second one to give.
            tx.rollback().await.map_err(backend)?;
            return Err(self.missing(namespace, run_id).await);
        };
        let (status, epoch) = (parse_status(&status)?, to_u64(epoch, "epoch")?);
        if status.is_terminal() {
            return Err(StoreError::InvalidTransition {
                from: status,
                to: RunStatus::Running,
            });
        }
        let live = status == RunStatus::Running && left_ms > 0;
        if live && !force && holder.as_deref() != Some(self.holder.as_str()) {
            return Err(StoreError::RunLeased {
                run_id: run_id.clone(),
                owner: held_by.unwrap_or_default(),
                expires_in: Duration::from_millis(to_u64(left_ms, "lease")?),
            });
        }
        let epoch = epoch + 1;
        sqlx::query(CLAIM)
            .bind(namespace)
            .bind(run_id.as_str())
            .bind(status_name(RunStatus::Running))
            .bind(to_i64(epoch, "epoch")?)
            .bind(owner)
            .bind(&self.holder)
            .bind(self.ttl_ms())
            .execute(&mut *tx)
            .await
            .map_err(backend)?;
        tx.commit().await.map_err(backend)?;
        Ok(Lease {
            namespace: namespace.to_owned(),
            run_id: run_id.clone(),
            owner: owner.to_owned(),
            epoch,
        })
    }

    /// Run [`RENEW`] (with the TTL) or [`RELEASE`] under `lease`'s epoch;
    /// whether a row matched. A serialization failure wrote nothing and is
    /// tried again.
    async fn lease_write(
        &self,
        sql: &'static str,
        ttl_ms: Option<f64>,
        lease: &Lease,
    ) -> Result<bool, StoreError> {
        // No row holds an epoch past i64::MAX.
        let Ok(epoch) = i64::try_from(lease.epoch) else {
            return Ok(false);
        };
        let mut cause = None;
        for _ in 0..crate::WRITE_TRIES {
            let query = || {
                let query = (sqlx::query(sql).bind(&lease.namespace))
                    .bind(lease.run_id.as_str())
                    .bind(epoch);
                match ttl_ms {
                    Some(ms) => query.bind(ms),
                    None => query,
                }
            };
            match self.line.execute(query).await {
                Ok(rows) => return Ok(rows == 1),
                Err(error) if crate::serialization_failure(&error) => cause = Some(error),
                Err(error) => return Err(backend(error)),
            }
        }
        Err(backend(cause.expect("WRITE_TRIES is not 0")))
    }

    /// Insert the run row and its first events in one transaction.
    async fn insert(&self, run: &NewRun, checkpoint: &str) -> Result<Insert, StoreError> {
        let events = encode_events(&run.events)?;
        let last_seq = run.events.len() as u64;
        let mut tx = crate::begin(&self.pool).await.map_err(backend)?;
        // Only an existing id is skipped here; a live run on the session
        // fails the statement through the partial unique index.
        let inserted = sqlx::query(
            "INSERT INTO linch_runs (namespace, run_id, session_id, parent_run_id, contract, \
             status, epoch, checkpoint, last_seq, principal) \
             VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, $9) \
             ON CONFLICT (namespace, run_id) DO NOTHING",
        )
        .bind(&run.namespace)
        .bind(run.run_id.as_str())
        .bind(run.session_id.as_str())
        .bind(run.parent_run_id.as_ref().map(RunId::as_str))
        .bind(run.contract.to_string())
        .bind(status_name(RunStatus::Pending))
        .bind(checkpoint)
        .bind(to_i64(last_seq, "last_seq")?)
        .bind(encode_principal(run.principal.as_ref())?)
        .execute(&mut *tx)
        .await;
        let inserted = match inserted {
            Ok(done) => done.rows_affected(),
            Err(error) if session_busy(&error) => return Ok(Insert::Busy(error)),
            Err(error) => return Err(backend(error)),
        };
        if inserted == 0 {
            return Err(StoreError::Conflict {
                message: format!("run {} already exists in {}", run.run_id, run.namespace),
            });
        }
        insert_events(&mut tx, &run.namespace, &run.run_id, events).await?;
        tx.commit().await.map_err(backend)?;
        Ok(Insert::Done)
    }
}

#[async_trait]
impl RunStore for PgRunStore {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        check_events(&run.run_id, 0, &run.events)?;
        let checkpoint = encode_checkpoint(&run.checkpoint)?;
        let mut tries = 0;
        while let Insert::Busy(error) = self.insert(&run, &checkpoint).await? {
            let live: Option<String> = sqlx::query_scalar(LIVE_RUN)
                .bind(&run.namespace)
                .bind(run.session_id.as_str())
                .fetch_optional(&self.pool)
                .await
                .map_err(backend)?;
            if let Some(live) = live {
                return Err(StoreError::SessionBusy {
                    session_id: run.session_id,
                    run_id: RunId::new(live),
                });
            }
            // The live run ended between the insert and the read.
            tries += 1;
            if tries == ADMIT_TRIES {
                return Err(backend(error));
            }
        }
        let last_seq = run.events.len() as u64;
        Ok(RunRecord {
            namespace: run.namespace,
            run_id: run.run_id,
            session_id: run.session_id,
            parent_run_id: run.parent_run_id,
            principal: run.principal,
            contract: run.contract,
            status: RunStatus::Pending,
            epoch: 0,
            checkpoint: run.checkpoint,
            last_seq,
        })
    }

    async fn claim(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError> {
        self.take(namespace, run_id, owner, false).await
    }

    async fn seize(
        &self,
        namespace: &str,
        run_id: &RunId,
        owner: &str,
    ) -> Result<Lease, StoreError> {
        self.take(namespace, run_id, owner, true).await
    }

    fn lease_ttl(&self) -> Option<Duration> {
        Some(self.lease_ttl)
    }

    async fn renew(&self, lease: &Lease) -> Result<(), StoreError> {
        match self.lease_write(RENEW, Some(self.ttl_ms()), lease).await? {
            true => Ok(()),
            false => Err(StoreError::LeaseLost {
                run_id: lease.run_id.clone(),
                epoch: lease.epoch,
            }),
        }
    }

    async fn release(&self, lease: &Lease) -> Result<(), StoreError> {
        self.lease_write(RELEASE, None, lease).await.map(|_| ())
    }

    async fn load(&self, namespace: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        let row: Option<RecordRow> = sqlx::query_as(
            "SELECT session_id, parent_run_id, principal, contract, status, epoch, checkpoint, \
             last_seq FROM linch_runs WHERE namespace = $1 AND run_id = $2",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(backend)?;
        let Some((
            session_id,
            parent_run_id,
            principal,
            contract,
            status,
            epoch,
            checkpoint,
            last_seq,
        )) = row
        else {
            return Err(self.missing(namespace, run_id).await);
        };
        Ok(RunRecord {
            namespace: namespace.to_owned(),
            run_id: run_id.clone(),
            session_id: session_id.into(),
            parent_run_id: parent_run_id.map(RunId::from),
            principal: decode_principal(principal.as_deref())?,
            contract: parse_contract(&contract)?,
            status: parse_status(&status)?,
            epoch: to_u64(epoch, "epoch")?,
            checkpoint: decode_checkpoint(&checkpoint)?,
            last_seq: to_u64(last_seq, "last_seq")?,
        })
    }

    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        let checkpoint = commit
            .checkpoint
            .as_ref()
            .map(encode_checkpoint)
            .transpose()?;
        let events = encode_events(&commit.events)?;
        // A commit that no run row takes skips the write; the read says why.
        let after = (commit.events.first()).map_or(0, |event| event.seq.saturating_sub(1));
        let fits = commit.status != Some(RunStatus::Pending)
            && check_events(&lease.run_id, after, &commit.events).is_ok();
        let (namespace, run_id) = (&lease.namespace, &lease.run_id);
        let mut cause = String::new();
        for _ in 0..crate::WRITE_TRIES {
            let written = match fits {
                true => (self.write(lease, &commit, checkpoint.as_deref(), &events)).await,
                false => Ok(false),
            };
            match written {
                Ok(true) => return Ok(()),
                Ok(false) => {
                    let state = run_state(&self.pool, namespace, run_id).await?;
                    // `Ok`: the row moved after the refused write.
                    refusal(lease, &commit, state)?;
                }
                Err(error) if crate::serialization_failure(&error) => {
                    cause = format!(" (last: {error})");
                }
                Err(error) => return Err(backend(error)),
            }
        }
        Err(StoreError::Conflict {
            message: format!("run {} kept changing during a commit{cause}", lease.run_id),
        })
    }

    async fn events(
        &self,
        namespace: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        // The left join yields one row with a NULL envelope for a run with no
        // matching events, and no row at all for a missing run.
        let rows: Vec<(Option<String>,)> = sqlx::query_as(
            "SELECT e.envelope FROM linch_runs r \
             LEFT JOIN linch_run_events e \
             ON e.namespace = r.namespace AND e.run_id = r.run_id AND e.seq > $3 \
             WHERE r.namespace = $1 AND r.run_id = $2 ORDER BY e.seq",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .bind(i64::try_from(after_seq).unwrap_or(i64::MAX))
        .fetch_all(&self.pool)
        .await
        .map_err(backend)?;
        if rows.is_empty() {
            return Err(self.missing(namespace, run_id).await);
        }
        rows.into_iter()
            .filter_map(|(text,)| text)
            .map(|text| decode_event(&text))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The SQL lists the terminal statuses by name; they must be exactly
    /// the ones linch-core calls terminal.
    #[test]
    fn the_live_run_predicate_matches_core() {
        use RunStatus::*;
        let predicate = "status NOT IN ('completed', 'failed', 'aborted')";
        let terminal = ["completed", "failed", "aborted"];
        let all = [
            Pending,
            Running,
            AwaitingApproval,
            AwaitingInput,
            Completed,
            Failed,
            Aborted,
        ];
        for status in all {
            let listed = terminal.contains(&status_name(status));
            assert_eq!(status.is_terminal(), listed, "{status:?}");
        }
        let index = include_str!("../migrations/0002_one_live_run_per_session.sql");
        assert!(index.contains(predicate) && LIVE_RUN.contains(predicate));
    }
}
