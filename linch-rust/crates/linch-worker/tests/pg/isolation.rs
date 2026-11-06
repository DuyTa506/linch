//! The worker pins READ COMMITTED. Under a REPEATABLE READ default, the
//! count taken after a limited queue's lock would still see the snapshot
//! from before the lock wait, miss the job the lock's last holder took, and
//! run more jobs than the limit.

use std::time::{Duration, Instant};

use linch_core::{SessionId, ToolScope};
use linch_worker::{QueueLimit, enqueue_run};
use sqlx::postgres::PgPoolOptions;

use crate::common::*;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_queue_limit_holds_under_repeatable_read() {
    let Some(db) = TestDb::new("rr_limit").await else {
        return;
    };
    for session in ["s1", "s2"] {
        let session = SessionId::new(session);
        enqueue_run(&db.pool, "narrow", NS, &session, "go")
            .await
            .unwrap();
    }
    // Another worker holds the queue's lock and takes the second job...
    let mut holder = db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('linch_worker_queue:narrow', 0))")
        .execute(&mut *holder)
        .await
        .unwrap();
    sqlx::query(
        "UPDATE linch_worker_jobs SET owner = 'other', attempts = 1, \
         visible_at = now() + interval '1 minute' WHERE session_id = 's2'",
    )
    .execute(&mut *holder)
    .await
    .unwrap();

    // ...while this worker's dequeue waits for that lock.
    let options =
        (db.options.clone()).options([("default_transaction_isolation", "repeatable read")]);
    let pool = PgPoolOptions::new()
        .max_connections(8)
        .connect_with(options)
        .await
        .unwrap();
    let mut narrow = config("w1");
    narrow.queues = vec![QueueLimit {
        name: "narrow".into(),
        limit: 1,
    }];
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&pool, narrow, agent(&pool, probe.clone()));
    let deadline = Instant::now() + Duration::from_secs(10);
    // pg_locks, unlike pg_stat_activity, is not a snapshot per transaction.
    while sqlx::query_scalar::<_, Option<i32>>(
        "SELECT pid FROM pg_locks WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))",
    )
    .fetch_optional(&mut *holder)
    .await
    .unwrap()
    .is_none()
    {
        assert!(Instant::now() < deadline, "the dequeue never waited");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    holder.commit().await.unwrap();

    // The queue is full, so the first job stays queued.
    tokio::time::sleep(TIMEOUT / 2).await;
    assert_eq!(
        probe.started(),
        0,
        "a second job of a queue limited to one ran"
    );
    let owner: Option<Option<String>> =
        sqlx::query_scalar("SELECT owner FROM linch_worker_jobs WHERE session_id = 's1'")
            .fetch_optional(&db.pool)
            .await
            .unwrap();
    assert_eq!(owner, Some(None), "the first job is still queued");
    worker.stop().await;
    pool.close().await;
    db.finish().await;
}
