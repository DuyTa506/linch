//! `run_workflow`, journaled steps, resume and the replay rule.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::sync::Arc;

use linch_core::events::kinds::WorkflowKind;
use serde_json::json;

use super::*;

fn ok<T>(value: T) -> std::future::Ready<Result<T, Infallible>> {
    std::future::ready(Ok(value))
}

fn assert_send<T: Send>(value: T) -> T {
    value
}

#[tokio::test]
async fn a_workflow_future_is_send_so_a_host_can_spawn_it() {
    let store = Arc::new(MemoryJournalStore::default());
    let run = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let fetched = wf.step("fetch", || ok(1u8));
            let answer = wf.interrupt::<u8>("ask", json!(null));
            let both = wf.settled([Box::pin(fetched) as Branch<'_>, Box::pin(answer)]);
            both.await
        },
    );
    let result = assert_send(run).await;
    assert!(matches!(result, Err(WorkflowError::Suspended(_))));
}

type Branch<'a> =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<u8, WorkflowError>> + Send + 'a>>;

#[tokio::test]
async fn run_workflow_returns_the_function_value_and_marks_it_completed() {
    let store = Arc::new(MemoryJournalStore::default());
    let recorder = Recorder::default();
    let value = run_workflow(options(&store, "w1", &recorder), |wf| async move {
        wf.phase("Find");
        Ok("done")
    })
    .await
    .unwrap();

    assert_eq!(value, "done");
    let run = stored(&store, "w1").await;
    assert_eq!((run.status, run.epoch), (WorkflowStatus::Completed, 1));
    let events = recorder.events();
    assert_eq!(recorder.kinds(), [WorkflowKind::Phase]);
    assert_eq!(events[0].title, "Find");
}

#[tokio::test]
async fn a_step_is_journaled_and_replayed_without_running_again() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move {
            let n = wf.step("count", || ok(runs.bump())).await?;
            Ok(n * 10)
        }
    };
    let first = Recorder::default();
    let value = run_workflow(options(&store, "w1", &first), flow).await;
    assert_eq!(value.unwrap(), 10);
    assert_eq!(
        first.kinds(),
        [WorkflowKind::StepStart, WorkflowKind::StepEnd]
    );
    let end = &first.events()[1];
    assert_eq!(end.result_text.as_deref(), Some("1"));
    assert_eq!(end.call_key, step_key("count", None).unwrap());

    let second = Recorder::default();
    let value = run_workflow(options(&store, "w1", &second), flow).await;
    assert_eq!(value.unwrap(), 10);
    assert_eq!(runs.get(), 1);
    assert_eq!(second.kinds(), [WorkflowKind::StepReplayed]);
    assert_eq!(stored(&store, "w1").await.epoch, 2);
}

#[tokio::test]
async fn steps_in_a_loop_replay_per_occurrence_and_keys_separate_inputs() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move {
            let mut seen = Vec::new();
            for url in ["a", "b"] {
                let options = StepOptions {
                    key: Some(json!(url)),
                    ..StepOptions::default()
                };
                let fetched = wf.step_with("fetch", &options, || ok(format!("{url}!")));
                seen.push(fetched.await?);
            }
            for _ in 0..2 {
                seen.push(wf.step("tick", || ok(runs.bump().to_string())).await?);
            }
            Ok(seen)
        }
    };
    let recorder = Recorder::default();
    let first = run_workflow(options(&store, "w1", &recorder), flow).await;
    assert_eq!(first.unwrap(), ["a!", "b!", "1", "2"]);
    let events = recorder.events();
    assert_ne!(events[1].call_key, events[3].call_key);
    assert_eq!((events[5].occurrence, events[7].occurrence), (0, 1));

    let again = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert_eq!(again.unwrap(), ["a!", "b!", "1", "2"]);
    assert_eq!(runs.get(), 2);
    assert_eq!(stored(&store, "w1").await.records.len(), 4);
}

#[tokio::test]
async fn a_renamed_step_reruns_while_unchanged_later_steps_replay() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |name: &'static str| {
        let runs = runs.clone();
        move |wf: WorkflowContext| async move {
            let a = wf.step(name, || ok(runs.bump())).await?;
            let b = wf.step("last", || ok(runs.bump())).await?;
            Ok((a, b))
        }
    };
    let first = run_workflow(options(&store, "w1", &Recorder::default()), flow("first")).await;
    assert_eq!(first.unwrap(), (1, 2));

    let recorder = Recorder::default();
    let renamed = run_workflow(options(&store, "w1", &recorder), flow("renamed")).await;
    assert_eq!(renamed.unwrap(), (3, 2));
    assert_eq!(
        recorder.kinds(),
        [
            WorkflowKind::StepStart,
            WorkflowKind::StepEnd,
            WorkflowKind::StepReplayed
        ]
    );
}

#[tokio::test]
async fn a_step_record_is_durable_before_the_next_step_runs() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |fail: bool| {
        let (store, runs) = (store.clone(), runs.clone());
        move |wf: WorkflowContext| async move {
            let first = wf.step("first", || ok(runs.bump())).await?;
            let records = stored(&store, "w1").await.records;
            assert_eq!(records.len(), 1, "first is durable before second runs");
            let second = wf
                .step("second", || {
                    let runs = runs.clone();
                    async move {
                        if fail {
                            return Err("publish failed".into());
                        }
                        Ok::<_, BoxError>(runs.bump())
                    }
                })
                .await?;
            Ok(first + second)
        }
    };
    let failed = run_workflow(options(&store, "w1", &Recorder::default()), flow(true)).await;
    let error = failed.unwrap_err();
    assert!(matches!(&error, WorkflowError::Step { name, .. } if name == "second"));
    assert_eq!(error.to_string(), "step second failed: publish failed");
    let run = stored(&store, "w1").await;
    assert_eq!(run.status, WorkflowStatus::Failed);
    assert_eq!(run.records.len(), 1);

    let resumed = run_workflow(options(&store, "w1", &Recorder::default()), flow(false)).await;
    assert_eq!(resumed.unwrap(), 3);
    assert_eq!(runs.get(), 2);
    assert_eq!(stored(&store, "w1").await.status, WorkflowStatus::Completed);
}

#[tokio::test]
async fn a_step_value_round_trips_through_json() {
    let store = Arc::new(MemoryJournalStore::default());
    let value = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let map = BTreeMap::from([("b".to_owned(), 2.5), ("a".to_owned(), 1.0)]);
            wf.step("map", || ok(map.clone())).await
        },
    )
    .await
    .unwrap();
    assert_eq!(value["a"], 1.0);
    let records = stored(&store, "w1").await.records;
    assert_eq!(records[0].value, json!({"a": 1.0, "b": 2.5}));
}

#[tokio::test]
async fn a_value_that_cannot_replay_fails_and_is_not_journaled() {
    let store = Arc::new(MemoryJournalStore::default());
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move { wf.step("nan", || ok(f64::NAN)).await },
    )
    .await;
    let error = result.unwrap_err();
    assert!(matches!(error, WorkflowError::Encode { .. }), "{error}");
    assert!(stored(&store, "w1").await.records.is_empty());
}

#[tokio::test]
async fn a_later_claim_fences_the_earlier_run_out() {
    let store = Arc::new(MemoryJournalStore::default());
    let outer = options(&store, "w1", &Recorder::default());
    let other = store.clone();
    let result = run_workflow(outer, |wf| async move {
        wf.step("first", || ok(1)).await?;
        // Another process takes the workflow over while this one runs.
        let inner = options(&other, "w1", &Recorder::default());
        let taken = run_workflow(inner, |wf| async move { wf.step("first", || ok(2)).await });
        assert_eq!(taken.await.unwrap(), 1);
        wf.step("second", || ok(3)).await
    })
    .await;
    let error = result.unwrap_err();
    assert!(
        matches!(
            error,
            WorkflowError::Store(JournalStoreError::LeaseLost { epoch: 1, .. })
        ),
        "{error}"
    );
    let run = stored(&store, "w1").await;
    assert_eq!((run.status, run.epoch), (WorkflowStatus::Completed, 2));
    assert_eq!(run.records.len(), 1);
}
