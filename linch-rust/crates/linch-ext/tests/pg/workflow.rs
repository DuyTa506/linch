//! Workflows across processes: each simulated process has its own pool and
//! store on one schema.

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use linch_core::events::kinds::WorkflowKind;
use linch_ext::workflow::{
    Interrupt, JournalStore, JournalStoreError, PgJournalStore, WorkflowContext, WorkflowError,
    WorkflowOptions, WorkflowStatus, run_workflow,
};
use serde_json::json;

use crate::common::TestDb;

const NS: &str = "tenant";

/// A process: a store on its own pool.
async fn process(db: &mut TestDb) -> Arc<PgJournalStore> {
    Arc::new(PgJournalStore::new(db.pool().await))
}

fn options(store: &Arc<PgJournalStore>) -> WorkflowOptions {
    WorkflowOptions::new(store.clone(), NS, "migrate-42")
}

async fn approval_flow(wf: WorkflowContext, runs: &AtomicUsize) -> Result<String, WorkflowError> {
    let plan = wf
        .step("plan", || async {
            runs.fetch_add(1, Ordering::SeqCst);
            Ok::<_, Infallible>("drop table".to_owned())
        })
        .await?;
    let approved: bool = wf.interrupt("approve", json!({"plan": plan})).await?;
    let outcome = if approved { "applied" } else { "cancelled" };
    Ok(outcome.to_owned())
}

#[tokio::test]
async fn a_suspended_workflow_is_answered_from_another_process() {
    let Some(mut db) = TestDb::migrated("answer").await else {
        return;
    };
    let runs = AtomicUsize::new(0);
    let first = process(&mut db).await;
    let result = run_workflow(options(&first), |wf| approval_flow(wf, &runs)).await;
    assert!(matches!(result, Err(WorkflowError::Suspended(_))));

    let reviewer = process(&mut db).await;
    let stored = reviewer.load(NS, "migrate-42").await.unwrap().unwrap();
    let question = Interrupt {
        key: "approve".into(),
        payload: json!({"plan": "drop table"}),
    };
    assert_eq!(stored.status, WorkflowStatus::AwaitingInput(question));
    let mut answered = options(&reviewer);
    answered.resume = HashMap::from([("approve".to_owned(), json!(true))]);
    let resumed = run_workflow(answered, |wf| approval_flow(wf, &runs)).await;
    assert_eq!(resumed.unwrap(), "applied");
    assert_eq!(runs.load(Ordering::SeqCst), 1);

    let later = process(&mut db).await;
    let kinds = Arc::new(Mutex::new(Vec::new()));
    let mut replaying = options(&later);
    let seen = kinds.clone();
    replaying.on_event = Some(Arc::new(move |event| seen.lock().unwrap().push(event.kind)));
    let replayed = run_workflow(replaying, |wf| approval_flow(wf, &runs)).await;
    assert_eq!(replayed.unwrap(), "applied");
    assert_eq!(
        *kinds.lock().unwrap(),
        [WorkflowKind::StepReplayed, WorkflowKind::InterruptReplayed]
    );
    let run = later.load(NS, "migrate-42").await.unwrap().unwrap();
    assert_eq!((run.status, run.epoch), (WorkflowStatus::Completed, 3));
    db.finish().await;
}

#[tokio::test]
async fn a_second_process_fences_the_first_out() {
    let Some(mut db) = TestDb::migrated("fence").await else {
        return;
    };
    let (first, second) = (process(&mut db).await, process(&mut db).await);
    let result = run_workflow(options(&first), |wf| async move {
        wf.step("one", || async { Ok::<_, Infallible>(1) }).await?;
        let taken = run_workflow(options(&second), |wf| async move {
            wf.step("one", || async { Ok::<_, Infallible>(2) }).await
        });
        assert_eq!(taken.await.unwrap(), 1, "the second process replays");
        wf.step("two", || async { Ok::<_, Infallible>(3) }).await
    })
    .await;
    assert!(matches!(
        result,
        Err(WorkflowError::Store(JournalStoreError::LeaseLost {
            epoch: 1,
            ..
        }))
    ));
    let run = first.load(NS, "migrate-42").await.unwrap().unwrap();
    assert_eq!(
        (run.status, run.records.len()),
        (WorkflowStatus::Completed, 1)
    );
    db.finish().await;
}

#[tokio::test]
async fn a_crashed_workflow_resumes_in_a_new_process() {
    let Some(mut db) = TestDb::migrated("crash").await else {
        return;
    };
    let runs = AtomicUsize::new(0);
    let flow = |wf: WorkflowContext, hang: bool| {
        let runs = &runs;
        async move {
            let one = wf
                .step("one", || async {
                    Ok::<_, Infallible>(runs.fetch_add(1, Ordering::SeqCst) + 1)
                })
                .await?;
            let two = wf
                .step("two", || async move {
                    if hang {
                        std::future::pending::<()>().await;
                    }
                    Ok::<_, Infallible>(10)
                })
                .await?;
            Ok(one + two)
        }
    };
    let crashed = process(&mut db).await;
    let run = run_workflow(options(&crashed), |wf| flow(wf, true));
    // Dropping the future mid-step is a crash: no status write follows.
    assert!(
        tokio::time::timeout(Duration::from_millis(200), run)
            .await
            .is_err()
    );
    let stored = crashed.load(NS, "migrate-42").await.unwrap().unwrap();
    assert_eq!(
        (stored.status, stored.records.len()),
        (WorkflowStatus::Running, 1)
    );

    let fresh = process(&mut db).await;
    let resumed = run_workflow(options(&fresh), |wf| flow(wf, false)).await;
    assert_eq!(resumed.unwrap(), 11);
    assert_eq!(runs.load(Ordering::SeqCst), 1);
    db.finish().await;
}
