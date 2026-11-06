//! [`PgRunStore`]: runs, checkpoints and the event journal in Postgres.
//!
//! Every write is one transaction that first locks the run row, then applies
//! the lifecycle checks in Rust, in linch-core's order: not found, lease
//! epoch, status, event run ids, then seqs. A rejected or interrupted write
//! rolls back and changes nothing.

use async_trait::async_trait;
use linch_core::{
    Commit, Envelope, Lease, NewRun, RunId, RunRecord, RunStatus, RunStore, StoreError,
};
use sqlx::{PgPool, Postgres, Transaction};

use crate::codec::{
    backend, check_events, decode_checkpoint, decode_event, encode_checkpoint, encode_events,
    not_found, parse_contract, parse_status, status_name, to_i64, to_u64,
};

/// A [`RunStore`] backed by Postgres.
///
/// Records are keyed by `(namespace, run_id)`. Run [`crate::migrate`] on the
/// database before the first call.
#[derive(Debug, Clone)]
pub struct PgRunStore {
    pool: PgPool,
}

impl PgRunStore {
    /// A store that runs its queries on `pool`.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

type RecordRow = (String, Option<String>, String, String, i64, String, i64);

/// Status, epoch and last seq of a locked run row.
async fn lock_run(
    tx: &mut Transaction<'static, Postgres>,
    namespace: &str,
    run_id: &RunId,
) -> Result<(RunStatus, u64, u64), StoreError> {
    let row: Option<(String, i64, i64)> = sqlx::query_as(
        "SELECT status, epoch, last_seq FROM linch_runs \
         WHERE namespace = $1 AND run_id = $2 FOR UPDATE",
    )
    .bind(namespace)
    .bind(run_id.as_str())
    .fetch_optional(&mut **tx)
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

#[async_trait]
impl RunStore for PgRunStore {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        check_events(&run.run_id, 0, &run.events)?;
        let checkpoint = encode_checkpoint(&run.checkpoint)?;
        let events = encode_events(&run.events)?;
        let last_seq = run.events.len() as u64;
        let mut tx = self.pool.begin().await.map_err(backend)?;
        let inserted = sqlx::query(
            "INSERT INTO linch_runs (namespace, run_id, session_id, parent_run_id, contract, \
             status, epoch, checkpoint, last_seq) \
             VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8) ON CONFLICT DO NOTHING",
        )
        .bind(&run.namespace)
        .bind(run.run_id.as_str())
        .bind(run.session_id.as_str())
        .bind(run.parent_run_id.as_ref().map(RunId::as_str))
        .bind(run.contract.to_string())
        .bind(status_name(RunStatus::Pending))
        .bind(checkpoint)
        .bind(to_i64(last_seq, "last_seq")?)
        .execute(&mut *tx)
        .await
        .map_err(backend)?
        .rows_affected();
        if inserted == 0 {
            return Err(StoreError::Conflict {
                message: format!("run {} already exists in {}", run.run_id, run.namespace),
            });
        }
        insert_events(&mut tx, &run.namespace, &run.run_id, events).await?;
        tx.commit().await.map_err(backend)?;
        Ok(RunRecord {
            namespace: run.namespace,
            run_id: run.run_id,
            session_id: run.session_id,
            parent_run_id: run.parent_run_id,
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
        let mut tx = self.pool.begin().await.map_err(backend)?;
        let (status, epoch, _) = lock_run(&mut tx, namespace, run_id).await?;
        if status.is_terminal() {
            return Err(StoreError::InvalidTransition {
                from: status,
                to: RunStatus::Running,
            });
        }
        let epoch = epoch + 1;
        sqlx::query(
            "UPDATE linch_runs SET status = $3, epoch = $4, owner = $5 \
             WHERE namespace = $1 AND run_id = $2",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .bind(status_name(RunStatus::Running))
        .bind(to_i64(epoch, "epoch")?)
        .bind(owner)
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

    async fn load(&self, namespace: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        let row: Option<RecordRow> = sqlx::query_as(
            "SELECT session_id, parent_run_id, contract, status, epoch, checkpoint, last_seq \
             FROM linch_runs WHERE namespace = $1 AND run_id = $2",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(backend)?;
        let (session_id, parent_run_id, contract, status, epoch, checkpoint, last_seq) =
            row.ok_or_else(|| not_found("run", namespace, run_id.as_str()))?;
        Ok(RunRecord {
            namespace: namespace.to_owned(),
            run_id: run_id.clone(),
            session_id: session_id.into(),
            parent_run_id: parent_run_id.map(RunId::from),
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
        let (namespace, run_id) = (&lease.namespace, &lease.run_id);
        let mut tx = self.pool.begin().await.map_err(backend)?;
        let (from, epoch, last_seq) = lock_run(&mut tx, namespace, run_id).await?;
        if lease.epoch != epoch {
            return Err(StoreError::LeaseLost {
                run_id: run_id.clone(),
                epoch: lease.epoch,
            });
        }
        let to = commit.status.unwrap_or(from);
        if from != RunStatus::Running || to == RunStatus::Pending {
            return Err(StoreError::InvalidTransition { from, to });
        }
        check_events(run_id, last_seq, &commit.events)?;
        let last_seq = last_seq + commit.events.len() as u64;
        // The run row first, so a crash during the event insert still has a
        // landed write to roll back.
        sqlx::query(
            "UPDATE linch_runs SET status = $3, checkpoint = COALESCE($4, checkpoint), \
             last_seq = $5 WHERE namespace = $1 AND run_id = $2",
        )
        .bind(namespace)
        .bind(run_id.as_str())
        .bind(status_name(to))
        .bind(checkpoint)
        .bind(to_i64(last_seq, "last_seq")?)
        .execute(&mut *tx)
        .await
        .map_err(backend)?;
        insert_events(&mut tx, namespace, run_id, events).await?;
        tx.commit().await.map_err(backend)
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
            return Err(not_found("run", namespace, run_id.as_str()));
        }
        rows.into_iter()
            .filter_map(|(text,)| text)
            .map(|text| decode_event(&text))
            .collect()
    }
}
