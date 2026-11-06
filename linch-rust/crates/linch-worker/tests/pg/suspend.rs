//! A run suspended for approval ends its job and waits; only an approve
//! job continues it.

use linch_core::hooks::ApprovalResponse;
use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{enqueue_approve, enqueue_resume, enqueue_run};

use crate::common::*;

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

    // Past a whole visibility timeout nothing picks the run up again.
    tokio::time::sleep(TIMEOUT + TIMEOUT / 2).await;
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
