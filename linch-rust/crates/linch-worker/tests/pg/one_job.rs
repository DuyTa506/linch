//! A run has at most one active job: a second job would claim the run from
//! the first, and every tool result in flight would become the interrupted
//! placeholder.

use std::sync::Arc;

use linch_core::hooks::ApprovalResponse;
use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{WorkerError, enqueue_approve, enqueue_resume, enqueue_run};
use tokio::sync::Semaphore;

use crate::common::*;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_run_has_one_active_job() {
    let Some(db) = TestDb::new("one_job").await else {
        return;
    };
    let gate = Arc::new(Semaphore::new(0));
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::Forever(Arc::clone(&gate)));
    let job = enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let workers = [
        Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone())),
        Running::spawn(&db.pool, config("w2"), agent(&db.pool, probe.clone())),
    ];
    wait_until("a worker runs the tool", || async { probe.started() == 1 }).await;
    let run_id = db.run_of("s1").await;

    // A resume of a run whose job is active is that job.
    let resume = enqueue_resume(&db.pool, "default", NS, &run_id).await;
    assert_eq!(resume.unwrap(), job);
    // Approving would leave the answers with a job that may never read
    // them, so it is refused.
    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    let approve = enqueue_approve(&db.pool, "default", NS, &run_id, approvals).await;
    assert!(
        matches!(&approve, Err(WorkerError::RunBusy { job: busy, .. }) if *busy == job),
        "{approve:?}"
    );
    assert_eq!(db.jobs().await, 1);

    gate.add_permits(1);
    wait_until("the run completes and its job ends", || async {
        db.status(&run_id).await == RunStatus::Completed && db.jobs().await == 0
    })
    .await;
    // Nothing took the run over: one claim, and the real tool output.
    assert_eq!(db.record(&run_id).await.epoch, 1);
    assert_eq!(db.tool_results("s1").await, vec!["v"]);

    // With the job gone, a resume queues a new one (which ends at once).
    let again = enqueue_resume(&db.pool, "default", NS, &run_id).await;
    assert_ne!(again.unwrap(), job);
    wait_until("the new job ends", || async { db.jobs().await == 0 }).await;
    for worker in workers {
        worker.stop().await;
    }
    db.finish().await;
}
