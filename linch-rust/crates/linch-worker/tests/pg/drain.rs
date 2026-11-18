//! A stop signal drains the jobs in flight for at most the drain timeout,
//! then hands them back: the run's lease and the job's hold are released at
//! once, so another worker need not wait out either.

use std::sync::Arc;
use std::time::Duration;

use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{Worker, enqueue_run};
use tokio::sync::Semaphore;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

use crate::common::*;

const DRAIN: Duration = Duration::from_millis(400);

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_job_that_never_finishes_is_handed_back_at_the_drain_timeout() {
    let Some(db) = TestDb::new("drain").await else {
        return;
    };
    // The tool waits on a permit that never comes.
    let hold = Hold::Forever(Arc::new(Semaphore::new(0)));
    let probe = Probe::new("Lookup", ToolScope::Read, hold);
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let mut stuck = config("w1");
    stuck.drain_timeout = DRAIN;
    let stop = CancellationToken::new();
    let worker = Worker::new(db.pool.clone(), stuck).agent(agent(&db.pool, probe.clone()));
    let handle = tokio::spawn(worker.run(stop.clone()));
    wait_until("the worker runs the tool", || async {
        probe.started() == 1
    })
    .await;
    let run_id = db.run_of("s1").await;

    let stopped = Instant::now();
    stop.cancel();
    let ended = tokio::time::timeout(DRAIN + Duration::from_secs(5), handle).await;
    let ended = ended.expect("run returns within the drain timeout and a margin");
    ended.unwrap().unwrap();
    assert!(
        stopped.elapsed() >= DRAIN,
        "it waited for the job before handing it back"
    );

    // Checked right after `run` returned, with a 30 s hold and a 1.5 s run
    // lease: only an explicit release makes both true now.
    let lease_free: bool =
        sqlx::query_scalar("SELECT lease_until IS NULL FROM linch_runs WHERE run_id = $1")
            .bind(run_id.to_string())
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(lease_free, "the run's lease is released");
    let (visible, failures, owner): (bool, i32, Option<String>) =
        sqlx::query_as("SELECT visible_at <= now(), failures, owner FROM linch_worker_jobs")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(visible, "the job is visible again at once");
    assert_eq!((failures, owner), (0, None), "no failure is counted");
    assert_eq!(db.status(&run_id).await, RunStatus::Running);

    // Another worker takes the job and the run over at once.
    let second = Running::spawn(
        &db.pool,
        config("w2"),
        agent(&db.pool, probe.with_hold(Hold::None)),
    );
    wait_until("the second worker completes the run", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    second.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_job_that_finishes_inside_the_drain_timeout_settles_normally() {
    let Some(db) = TestDb::new("drain_fast").await else {
        return;
    };
    let hold = Hold::Sleep(Duration::from_millis(300));
    let probe = Probe::new("Lookup", ToolScope::Read, hold);
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let mut roomy = config("w1");
    roomy.drain_timeout = Duration::from_secs(20);
    let stop = CancellationToken::new();
    let worker = Worker::new(db.pool.clone(), roomy).agent(agent(&db.pool, probe.clone()));
    let handle = tokio::spawn(worker.run(stop.clone()));
    wait_until("the worker runs the tool", || async {
        probe.started() == 1
    })
    .await;
    stop.cancel();
    handle.await.unwrap().unwrap();
    let run_id = db.run_of("s1").await;
    assert_eq!(db.status(&run_id).await, RunStatus::Completed);
    assert_eq!(db.jobs().await, 0, "the job is acked");
    db.finish().await;
}
