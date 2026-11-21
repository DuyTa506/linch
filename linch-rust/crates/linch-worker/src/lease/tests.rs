//! The fenced writes on Postgres under a REPEATABLE READ default. Each test
//! skips cleanly without `LINCH_TEST_PG_DSN`.

use std::future::Future;
use std::str::FromStr;
use std::time::{Duration, Instant};

use linch_core::{RunId, SessionId};
use sqlx::AssertSqlSafe;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

use super::*;
use crate::enqueue_run;

/// A migrated schema, with a pool at the server's default isolation and
/// one whose transactions default to REPEATABLE READ.
struct Db {
    admin: PgPool,
    pool: PgPool,
    repeatable_read: PgPool,
    schema: String,
}

impl Db {
    async fn new(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
            eprintln!("skipping {name}: LINCH_TEST_PG_DSN is not set");
            return None;
        };
        let schema = format!("worker_{name}");
        let admin = PgPoolOptions::new().max_connections(1).connect(&dsn).await;
        let admin = admin.expect("connect to the test database");
        let reset = format!("DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}");
        let query = sqlx::raw_sql(AssertSqlSafe(reset));
        query.execute(&admin).await.expect("reset the test schema");
        let options = PgConnectOptions::from_str(&dsn)
            .expect("a valid DSN")
            .options([("search_path", schema.as_str())]);
        let connect = |options: PgConnectOptions| async {
            let pool = PgPoolOptions::new().max_connections(2);
            pool.connect_with(options).await.expect("connect")
        };
        let pool = connect(options.clone()).await;
        let isolation = ("default_transaction_isolation", "repeatable read");
        let repeatable_read = connect(options.options([isolation])).await;
        crate::migrate(&pool).await.expect("worker migrations");
        Some(Self {
            admin,
            pool,
            repeatable_read,
            schema,
        })
    }

    /// A job of its own `queue`, dequeued on the REPEATABLE READ pool.
    async fn held(&self, queue: &str) -> JobLease {
        let session = SessionId::new(queue);
        let enqueued = enqueue_run(&self.pool, queue, "ns", &session, "go").await;
        enqueued.expect("enqueue");
        let served = Served {
            namespaces: vec!["ns".to_owned()],
            roots: Vec::new(),
        };
        let timeout = Duration::from_secs(30);
        let lease = dequeue(&self.repeatable_read, queue, &served, "w", timeout).await;
        lease.expect("dequeue").expect("a visible job")
    }

    /// Run `write` while another transaction holds an update of `lease`'s
    /// job row (the attempt's own heartbeat still in flight, say), and
    /// commit that transaction once `write` waits on it.
    async fn behind_an_update<T>(&self, lease: &JobLease, write: impl Future<Output = T>) -> T {
        let mut holder = self.pool.begin().await.unwrap();
        let updated = sqlx::query("UPDATE linch_worker_jobs SET error = error WHERE id = $1")
            .bind(lease.job.id.0)
            .execute(&mut *holder)
            .await
            .unwrap();
        assert_eq!(updated.rows_affected(), 1);
        let release = async {
            let deadline = Instant::now() + Duration::from_secs(10);
            // pg_locks, unlike pg_stat_activity, is not a snapshot per
            // transaction.
            while sqlx::query_scalar::<_, i32>(
                "SELECT pid FROM pg_locks \
                 WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))",
            )
            .fetch_optional(&mut *holder)
            .await
            .unwrap()
            .is_none()
            {
                assert!(Instant::now() < deadline, "the write never waited");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            holder.commit().await.unwrap();
        };
        let (result, ()) = tokio::join!(write, release);
        result
    }

    async fn finish(self) {
        self.pool.close().await;
        self.repeatable_read.close().await;
        let drop = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::raw_sql(AssertSqlSafe(drop));
        query
            .execute(&self.admin)
            .await
            .expect("drop the test schema");
    }
}

/// A fenced write that waited on another update of its job row lands once
/// that update commits. At REPEATABLE READ the wait ends in a serialization
/// error, which the write runs again; under READ COMMITTED it never does.
#[tokio::test]
async fn under_repeatable_read_a_fenced_write_behind_an_update_lands() {
    let Some(db) = Db::new("rr_fenced").await else {
        return;
    };
    let pool = &db.repeatable_read;
    let timeout = Duration::from_secs(30);
    let lease = db.held("heartbeat").await;
    let line = LeaseLine::new(pool, timeout / 3);
    let done = db.behind_an_update(&lease, lease.heartbeat(&line, timeout));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "heartbeat: {done:?}");
    let lease = db.held("record_run").await;
    let run_id = RunId::new("r1");
    let done = db.behind_an_update(&lease, lease.record_run(pool, &run_id));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "record_run: {done:?}");
    let lease = db.held("ack").await;
    let done = db.behind_an_update(&lease, lease.ack(pool, "done")).await;
    assert!(matches!(done, Ok(true)), "ack: {done:?}");
    let lease = db.held("release").await;
    let done = db.behind_an_update(&lease, lease.release(pool, timeout, "error", true));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "release: {done:?}");
    let lease = db.held("fail").await;
    let done = db.behind_an_update(&lease, lease.fail(pool, "error")).await;
    assert!(matches!(done, Ok(true)), "fail: {done:?}");
    db.finish().await;
}

/// The job heartbeat has a connection of its own: with every connection of
/// the pool held, it still lands, where a heartbeat in the pool would wait.
#[tokio::test]
async fn a_heartbeat_lands_while_the_pool_is_exhausted() {
    let Some(db) = Db::new("hb_pool").await else {
        return;
    };
    let lease = db.held("hb_pool").await;
    let timeout = Duration::from_secs(30);
    let line = LeaseLine::new(&db.repeatable_read, timeout / 3);
    let held = [
        db.repeatable_read.acquire().await.unwrap(),
        db.repeatable_read.acquire().await.unwrap(),
    ];
    let done = lease.heartbeat(&line, timeout).await;
    assert!(matches!(done, Ok(true)), "{done:?}");
    drop(held);
    db.finish().await;
}

/// A heartbeat that lands after this attempt gave its job back (the
/// keep-alive future is dropped with a heartbeat in flight and the server
/// still executes the statement) must not hide the job again: a job handed
/// back with no delay stays visible.
#[tokio::test]
async fn a_heartbeat_after_the_release_does_not_hide_the_job() {
    let Some(db) = Db::new("late_beat").await else {
        return;
    };
    let lease = db.held("late_beat").await;
    let timeout = Duration::from_secs(30);
    let line = LeaseLine::new(&db.pool, timeout / 3);
    let released = lease.release(&db.pool, Duration::ZERO, "the worker stopped", false);
    assert!(matches!(released.await, Ok(true)));
    let beat = lease.heartbeat(&line, timeout).await;
    let (hidden_ms, owner): (f64, Option<String>) = sqlx::query_as(
        "SELECT (extract(epoch FROM visible_at - now()) * 1000)::float8, owner \
         FROM linch_worker_jobs WHERE id = $1",
    )
    .bind(lease.job.id.0)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    db.finish().await;
    assert!(
        matches!(beat, Ok(false)) && hidden_ms <= 0.0,
        "a heartbeat after the release answered {beat:?} and left the job hidden for \
         {hidden_ms:.0} ms with owner {owner:?}"
    );
}

/// While the attempt owns the job, the heartbeat hides it for another
/// timeout.
#[tokio::test]
async fn a_heartbeat_while_the_job_is_owned_hides_it() {
    let Some(db) = Db::new("owned_beat").await else {
        return;
    };
    let lease = db.held("owned_beat").await;
    let timeout = Duration::from_secs(30);
    let line = LeaseLine::new(&db.pool, timeout / 3);
    let beat = lease.heartbeat(&line, timeout).await;
    let hidden_ms: f64 = sqlx::query_scalar(
        "SELECT (extract(epoch FROM visible_at - now()) * 1000)::float8 \
         FROM linch_worker_jobs WHERE id = $1",
    )
    .bind(lease.job.id.0)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    db.finish().await;
    assert!(matches!(beat, Ok(true)), "{beat:?}");
    assert!(hidden_ms > 20_000.0, "hidden for {hidden_ms:.0} ms");
}

/// The heartbeat matches `owner IS NOT NULL` and not
/// the owner's name. After a release, another worker's dequeue sets a new
/// owner and the next `attempts`; the first worker's late heartbeat must
/// answer `false` and leave the new holder's visibility alone.
#[tokio::test]
async fn a_heartbeat_after_another_workers_dequeue_changes_nothing() {
    let Some(db) = Db::new("other_beat").await else {
        return;
    };
    let first = db.held("late_beat").await;
    let line = LeaseLine::new(&db.pool, Duration::from_secs(10));
    let released = first.release(&db.pool, Duration::ZERO, "retry", true);
    assert!(matches!(released.await, Ok(true)));
    let served = Served {
        namespaces: vec!["ns".to_owned()],
        roots: Vec::new(),
    };
    // The second worker hides the job for 5 s only.
    let short = Duration::from_secs(5);
    let second = dequeue(&db.pool, "late_beat", &served, "w2", short).await;
    let second = second.unwrap().expect("the released job is visible");
    assert_eq!(second.attempt(), first.attempt() + 1);
    let read = || async {
        sqlx::query_as::<_, (f64, Option<String>)>(
            "SELECT (extract(epoch FROM visible_at - now()) * 1000)::float8, owner \
             FROM linch_worker_jobs WHERE id = $1",
        )
        .bind(first.job.id.0)
        .fetch_one(&db.pool)
        .await
        .unwrap()
    };
    let late = first.heartbeat(&line, Duration::from_secs(300)).await;
    let (hidden_ms, owner) = read().await;
    // The second worker fails the job for good: no heartbeat of either
    // attempt hides it again, and the first worker's other writes are
    // fenced by `attempts`.
    assert!(matches!(second.fail(&db.pool, "bad").await, Ok(true)));
    let after_fail = second.heartbeat(&line, Duration::from_secs(300)).await;
    let stale_release = first
        .release(&db.pool, Duration::ZERO, "stale", false)
        .await;
    let stale_ack = first.ack(&db.pool, "done").await;
    db.finish().await;
    assert!(matches!(late, Ok(false)), "{late:?}");
    assert!(hidden_ms <= 5_000.0, "hidden for {hidden_ms:.0} ms");
    assert_eq!(owner.as_deref(), Some("w2"));
    assert!(matches!(after_fail, Ok(false)), "{after_fail:?}");
    assert!(matches!(stale_release, Ok(false)), "{stale_release:?}");
    assert!(matches!(stale_ack, Ok(false)), "{stale_ack:?}");
}

/// The dequeue of a limited queue whose lock another transaction keeps does
/// not wait for it: it answers `None`, as for an empty queue, and the job
/// is taken at the next call once the lock is free.
#[tokio::test]
async fn a_limited_queue_whose_lock_is_held_elsewhere_is_skipped() {
    let Some(db) = Db::new("limited_lock").await else {
        return;
    };
    crate::set_queue_limit(&db.pool, "q", 2).await.unwrap();
    let session = SessionId::new("s1");
    enqueue_run(&db.pool, "q", "ns", &session, "go")
        .await
        .unwrap();
    let served = Served {
        namespaces: vec!["ns".to_owned()],
        roots: Vec::new(),
    };
    let timeout = Duration::from_secs(30);
    let mut holder = db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("{QUEUE_LOCK}q"))
        .execute(&mut *holder)
        .await
        .unwrap();
    let started = Instant::now();
    let skipped = dequeue(&db.pool, "q", &served, "w", timeout).await;
    let waited = started.elapsed();
    holder.rollback().await.unwrap();
    let taken = dequeue(&db.pool, "q", &served, "w", timeout).await;
    db.finish().await;
    assert!(matches!(skipped, Ok(None)), "{skipped:?}");
    assert!(waited < Duration::from_secs(5), "waited {waited:?}");
    assert!(matches!(taken, Ok(Some(_))), "{taken:?}");
}
