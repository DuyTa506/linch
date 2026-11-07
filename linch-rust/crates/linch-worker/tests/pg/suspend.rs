//! A run suspended for approval ends its job and waits; only an approve
//! job continues it.

use std::sync::Arc;
use std::time::Duration;

use futures::StreamExt;
use linch_core::hooks::ApprovalResponse;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::{RunError, RunOptions, RunStatus, SessionId, ToolScope};
use linch_worker::{enqueue_approve, enqueue_resume, enqueue_run};
use tokio::sync::Semaphore;

use crate::common::*;

async fn failed_job_error(db: &TestDb) -> Option<String> {
    sqlx::query_scalar("SELECT error FROM linch_worker_jobs WHERE failed_at IS NOT NULL")
        .fetch_optional(&db.pool)
        .await
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_suspended_run_waits_for_approve() {
    let Some(db) = TestDb::new("suspend").await else {
        return;
    };
    // Default permissions ask before a write tool runs.
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let run_id = db.run_of("s1").await;
    wait_until("the run suspends and its job ends", || async {
        db.status(&run_id).await == RunStatus::AwaitingApproval && db.jobs().await == 0
    })
    .await;

    // Its job is gone, so nothing picks the run up again.
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let record = db.record(&run_id).await;
    assert_eq!(
        (record.status, record.epoch),
        (RunStatus::AwaitingApproval, 1)
    );
    assert_eq!(db.jobs().await, 0);

    // A resume job does not run a suspended run either: it would only
    // suspend again.
    enqueue_resume(&db.pool, "default", NS, &run_id)
        .await
        .unwrap();
    wait_until("the resume job ends", || async { db.jobs().await == 0 }).await;
    let record = db.record(&run_id).await;
    assert_eq!(
        (record.status, record.epoch),
        (RunStatus::AwaitingApproval, 1)
    );
    assert_eq!(probe.started(), 0);

    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    enqueue_approve(&db.pool, "default", NS, &run_id, approvals)
        .await
        .unwrap();
    wait_until("the approved run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    assert_eq!(db.record(&run_id).await.epoch, 2);
    assert_eq!(probe.started(), 1);
    wait_until("the approve job is acked", || async {
        db.jobs().await == 0
    })
    .await;
    worker.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_late_approve_leaves_the_runs_owner_alone() {
    let Some(db) = TestDb::new("late_approve").await else {
        return;
    };
    // The host runs the run itself; its first model call waits at the gate.
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let gate = Arc::new(Gate::default());
    let host = gated_agent(&db.pool, probe.clone(), Arc::clone(&gate));
    let mut stream = host.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let hosting = tokio::spawn(async move {
        while let Some(item) = stream.next().await {
            item?;
        }
        Ok::<(), RunError>(())
    });
    gate.entered.notified().await;
    let epoch = db.record(&run_id).await.epoch;

    // An approve for it (the run is not waiting for one) fails its job and
    // claims nothing, so the host keeps the run.
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    enqueue_approve(&db.pool, "default", NS, &run_id, approvals)
        .await
        .unwrap();
    wait_until("the approve job fails", || async {
        failed_job_error(&db).await.is_some()
    })
    .await;
    let error = failed_job_error(&db).await.unwrap();
    assert!(error.contains("not awaiting approval"), "{error}");
    assert_eq!(
        db.record(&run_id).await.epoch,
        epoch,
        "nothing claimed the run"
    );
    gate.open.add_permits(2);
    hosting.await.unwrap().expect("the host's run");
    let record = db.record(&run_id).await;
    assert_eq!((record.status, record.epoch), (RunStatus::Completed, epoch));
    worker.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_approve_taken_over_after_its_answers_landed_resumes() {
    let Some(db) = TestDb::new("approve_takeover").await else {
        return;
    };
    let gate = Arc::new(Semaphore::new(0));
    let probe = Probe::new("Write", ToolScope::Write, Hold::Forever(gate));
    let first = Running::spawn(&db.pool, losing("w1"), agent(&db.pool, probe.clone()));
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let run_id = db.run_of("s1").await;
    wait_until("the run suspends and its job ends", || async {
        db.status(&run_id).await == RunStatus::AwaitingApproval && db.jobs().await == 0
    })
    .await;
    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    enqueue_approve(&db.pool, "default", NS, &run_id, approvals)
        .await
        .unwrap();
    // The approve's answers land and the tool starts; then its worker dies.
    wait_until("the approved tool starts", || async {
        probe.started() == 1
    })
    .await;
    first.kill().await;

    // The next attempt finds the run no longer suspended: its answers are
    // durable, so it resumes instead of failing as a late approve.
    let second = Running::spawn(
        &db.pool,
        config("w2"),
        agent(&db.pool, probe.with_hold(Hold::None)),
    );
    wait_until("the run completes and the job ends", || async {
        db.status(&run_id).await == RunStatus::Completed && db.jobs().await == 0
    })
    .await;
    assert_eq!(probe.started(), 1);
    assert_eq!(db.tool_results("s1").await, vec![INTERRUPTED_TOOL_TEXT]);
    second.stop().await;
    db.finish().await;
}
