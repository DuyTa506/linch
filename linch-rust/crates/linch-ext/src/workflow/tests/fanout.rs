//! Fan-out: `parallel` and `settled`.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use linch_core::events::kinds::WorkflowKind;
use serde_json::json;

use super::*;

async fn after(millis: u64, value: u64) -> Result<u64, BoxError> {
    tokio::time::sleep(Duration::from_millis(millis)).await;
    Ok(value)
}

async fn failing() -> Result<u64, BoxError> {
    Err("boom".into())
}

#[tokio::test]
async fn parallel_returns_values_in_input_order() {
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf: WorkflowContext| async move {
        let branches = [(30, 1), (20, 2), (10, 3)].map(|(millis, value)| {
            let wf = wf.clone();
            async move {
                let name = format!("branch-{value}");
                wf.step(&name, || after(millis, value)).await
            }
        });
        wf.parallel(branches).await
    };
    let value = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert_eq!(value.unwrap(), [1, 2, 3]);
    let recorder = Recorder::default();
    let replayed = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert_eq!(replayed.unwrap(), [1, 2, 3]);
    assert_eq!(recorder.kinds(), [WorkflowKind::StepReplayed; 3]);
}

#[tokio::test]
async fn identical_parallel_steps_replay_per_occurrence() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move {
            let tick = || {
                wf.step("tick", || {
                    std::future::ready(Ok::<_, BoxError>(runs.bump()))
                })
            };
            wf.parallel([tick(), tick(), tick()]).await
        }
    };
    let first = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    let first = first.unwrap();
    let replayed = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert_eq!(replayed.unwrap(), first);
    assert_eq!(runs.get(), 3);
}

#[tokio::test]
async fn parallel_runs_at_most_max_concurrency_branches_at_once() {
    let store = Arc::new(MemoryJournalStore::default());
    let mut capped = options(&store, "w1", &Recorder::default());
    capped.max_concurrency = 2;
    let (live, peak) = (AtomicUsize::new(0), AtomicUsize::new(0));
    let (live, peak) = (&live, &peak);
    let value = run_workflow(capped, |wf| async move {
        let branches = (0..5).map(|_| async move {
            let now = live.fetch_add(1, Ordering::SeqCst) + 1;
            peak.fetch_max(now, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(5)).await;
            live.fetch_sub(1, Ordering::SeqCst);
            Ok(())
        });
        wf.parallel(branches).await
    })
    .await;
    assert_eq!(value.unwrap().len(), 5);
    assert_eq!(peak.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn a_failing_branch_drops_its_siblings() {
    let store = Arc::new(MemoryJournalStore::default());
    let recorder = Recorder::default();
    let result = run_workflow(options(&store, "w1", &recorder), |wf| async move {
        let slow = wf.step("slow", || after(10_000, 1));
        let fail = wf.step("fail", failing);
        wf.parallel([Box::pin(slow) as BoxedStep<'_>, Box::pin(fail)])
            .await
    })
    .await;
    let error = result.unwrap_err();
    assert_eq!(error.to_string(), "step fail failed: boom");
    assert!(stored(&store, "w1").await.records.is_empty());
    assert!(!recorder.kinds().contains(&WorkflowKind::StepEnd));
}

#[tokio::test]
async fn nested_parallel_calls_do_not_deadlock() {
    let store = Arc::new(MemoryJournalStore::default());
    let mut single = options(&store, "w1", &Recorder::default());
    single.max_concurrency = 1;
    single.deadline = Some(Duration::from_secs(5));
    let value = run_workflow(single, |wf| async move {
        let outer = (0..2).map(|i| {
            let wf = wf.clone();
            async move {
                let inner = (0..2).map(|j| {
                    let name = format!("leaf-{i}-{j}");
                    let wf = wf.clone();
                    async move { wf.step(&name, || after(1, i * 10 + j)).await }
                });
                wf.parallel(inner).await
            }
        });
        wf.parallel(outer).await
    })
    .await;
    assert_eq!(value.unwrap(), [vec![0, 1], vec![10, 11]]);
}

#[tokio::test]
async fn settled_keeps_siblings_running_and_returns_every_outcome() {
    let store = Arc::new(MemoryJournalStore::default());
    let outcomes = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let fail = wf.step("fail", failing);
            let slow = wf.step("slow", || after(10, 7));
            wf.settled([Box::pin(fail) as BoxedStep<'_>, Box::pin(slow)])
                .await
        },
    )
    .await
    .unwrap();
    assert!(matches!(&outcomes[0], Err(WorkflowError::Step { name, .. }) if name == "fail"));
    assert_eq!(outcomes[1].as_ref().unwrap(), &7);
    let run = stored(&store, "w1").await;
    assert_eq!(
        (run.status, run.records.len()),
        (WorkflowStatus::Completed, 1)
    );
}

#[tokio::test]
async fn settled_propagates_a_suspend_after_its_siblings_finish() {
    let store = Arc::new(MemoryJournalStore::default());
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let ask = wf.interrupt::<u64>("pick", json!(["a", "b"]));
            let slow = wf.step("slow", || after(10, 7));
            wf.settled([Box::pin(ask) as BoxedStep<'_>, Box::pin(slow)])
                .await
        },
    )
    .await;
    assert!(matches!(result, Err(WorkflowError::Suspended(_))));
    let run = stored(&store, "w1").await;
    assert_eq!(run.records.len(), 1, "the sibling step finished");
    assert!(matches!(run.status, WorkflowStatus::AwaitingInput(_)));
}

#[tokio::test]
async fn an_interrupt_inside_parallel_drops_its_siblings() {
    let store = Arc::new(MemoryJournalStore::default());
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let slow = wf.step("slow", || after(10_000, 7));
            let ask = wf.interrupt::<u64>("pick", json!(null));
            wf.parallel([Box::pin(slow) as BoxedStep<'_>, Box::pin(ask)])
                .await
        },
    )
    .await;
    assert!(matches!(result, Err(WorkflowError::Suspended(_))));
    assert!(stored(&store, "w1").await.records.is_empty());
}

/// Branch 0 runs a slow `a`, then `fetch`; branch 1 runs `fetch` at once,
/// so live, branch 1 reaches `fetch` first, while on replay branch 0 does
/// (its `a` replays at once). `hang` holds branch 0's `fetch` forever, as
/// a crash before its record lands. `fetches` counts branch 1's calls.
async fn two_fetches(
    wf: WorkflowContext,
    hang: bool,
    fetches: Counter,
) -> Result<Vec<String>, WorkflowError> {
    let wf = &wf;
    let first = async move {
        let a = wf.step("a", || async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            Ok::<_, BoxError>("a0".to_owned())
        });
        let a = a.await?;
        let fetch = wf.step("fetch", || async move {
            if hang {
                std::future::pending::<()>().await;
            }
            Ok::<_, BoxError>("fetch-by-b0".to_owned())
        });
        Ok(format!("b0: a={a} fetch={}", fetch.await?))
    };
    let second = async move {
        let fetch = wf.step("fetch", || {
            fetches.bump();
            std::future::ready(Ok::<_, BoxError>("fetch-by-b1".to_owned()))
        });
        Ok(format!("b1: fetch={}", fetch.await?))
    };
    wf.parallel([Box::pin(first) as BoxedText<'_>, Box::pin(second)])
        .await
}

/// Parallel branches that share an unkeyed step name keep their own
/// records: on replay each branch gets its own, whichever reaches the name
/// first, so a journaled step never runs again after a crash.
#[tokio::test]
async fn branches_sharing_a_step_name_replay_their_own_records() {
    let store = Arc::new(MemoryJournalStore::default());
    let fetches = Counter::default();
    let options_for = |id| options(&store, id, &Recorder::default());
    let crashed = run_workflow(options_for("w1"), |wf| {
        two_fetches(wf, true, fetches.clone())
    });
    let recorded = async {
        let records = || async {
            let run = store.load(NS, "w1").await.unwrap();
            run.map_or(0, |run| run.records.len())
        };
        while records().await < 2 {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    };
    tokio::select! {
        outcome = crashed => panic!("branch 0's fetch never returns: {outcome:?}"),
        () = recorded => {}
    }
    let resumed = run_workflow(options_for("w1"), |wf| {
        two_fetches(wf, false, fetches.clone())
    });
    let texts = resumed.await.unwrap();
    assert_eq!(
        texts,
        ["b0: a=a0 fetch=fetch-by-b0", "b1: fetch=fetch-by-b1"]
    );
    assert_eq!(fetches.get(), 1, "branch 1's fetch ran once");
}

/// Branches of different future types, boxed into one list.
type BoxedText<'a> =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<String, WorkflowError>> + 'a>>;

/// Branches of different future types, boxed into one list.
type BoxedStep<'a> =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<u64, WorkflowError>> + Send + 'a>>;
