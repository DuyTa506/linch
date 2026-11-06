//! Retries back off, a job that keeps failing is failed for good (with its
//! run), and a start job waiting for its session is never given up on.

use std::sync::Arc;
use std::time::Duration;

use futures::StreamExt;
use linch_core::hooks::ApprovalResponse;
use linch_core::{RunOptions, RunStatus, SessionId, ToolScope};
use linch_worker::{enqueue_resume, enqueue_run};
use tokio::sync::Semaphore;
use tokio::time::Instant;

use crate::common::*;

/// `attempts`, `failures` and `error` of the only job, and whether it failed.
async fn job_row(db: &TestDb) -> (i32, i32, Option<String>, bool) {
    sqlx::query_as("SELECT attempts, failures, error, failed_at IS NOT NULL FROM linch_worker_jobs")
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

fn quick(owner: &str) -> linch_worker::WorkerConfig {
    let mut config = config(owner);
    config.retry_delay = Duration::from_millis(10);
    config
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_job_that_keeps_failing_is_failed_with_its_run() {
    let Some(db) = TestDb::new("dead_letter").await else {
        return;
    };
    // A run that stopped mid-tool, whose session then went missing: every
    // resume claims it and then cannot load the session.
    let held = Probe::new(
        "Lookup",
        ToolScope::Read,
        Hold::Forever(Arc::new(Semaphore::new(0))),
    );
    let mut stream = agent(&db.pool, held.clone()).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let running = tokio::spawn(async move { while stream.next().await.is_some() {} });
    wait_until("the tool starts", || async { held.started() == 1 }).await;
    running.abort();
    db.sql("DELETE FROM linch_session_entries").await;
    db.sql("DELETE FROM linch_sessions").await;

    let probe = held.with_hold(Hold::None);
    let worker = Running::spawn(&db.pool, quick("w1"), agent(&db.pool, probe));
    enqueue_resume(&db.pool, "default", NS, &run_id)
        .await
        .unwrap();
    wait_until("the job is failed for good", || async {
        job_row(&db).await.3
    })
    .await;
    let (attempts, _, error, _) = job_row(&db).await;
    let error = error.unwrap();
    assert_eq!(attempts, 5, "{error}");
    assert!(
        error.starts_with("gave up after 5 failed attempts"),
        "{error}"
    );
    assert!(error.contains("session s1 not found"), "{error}");
    // Its run can never continue either.
    assert_eq!(db.status(&run_id).await, RunStatus::Failed);
    worker.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_start_job_waits_for_a_busy_session_with_back_off() {
    let Some(db) = TestDb::new("busy").await else {
        return;
    };
    // The session's run waits for approval of a write.
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let host = agent(&db.pool, probe.clone());
    let mut stream = host.run("go", RunOptions::new("s1"));
    let first = stream.run_id().clone();
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    assert_eq!(db.status(&first).await, RunStatus::AwaitingApproval);

    let worker = Running::spawn(&db.pool, quick("w1"), agent(&db.pool, probe.clone()));
    let since = Instant::now();
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "again")
        .await
        .unwrap();
    // Past the failure cap, and never failed: a busy session is no failure.
    wait_until("eight attempts", || async { job_row(&db).await.0 >= 8 }).await;
    // Each wait doubles from 10 ms: 10 + 20 + ... + 640 ms before the 8th.
    assert!(
        since.elapsed() >= Duration::from_millis(1270),
        "{:?}",
        since.elapsed()
    );
    let (_, failures, error, failed) = job_row(&db).await;
    assert_eq!((failures, failed), (0, false));
    assert!(error.unwrap().contains("busy"));

    // Once the session's run ends, the waiting job starts its run (which
    // suspends at its own write).
    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)].into();
    let mut stream = host.approve(first.clone(), approvals, RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    assert_eq!(db.status(&first).await, RunStatus::Completed);
    wait_until("the second run starts and its job ends", || async {
        let runs = db.runs_of("s1").await;
        runs.len() == 2 && db.jobs().await == 0
    })
    .await;
    worker.stop().await;
    db.finish().await;
}
