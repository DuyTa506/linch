//! The failure policy: retries, step timeouts, the deadline and the cancel
//! token.

use std::sync::Arc;
use std::time::Duration;

use linch_core::events::kinds::WorkflowKind;

use super::*;

/// Three attempts with millisecond backoff.
fn retry() -> Retry {
    Retry {
        max_attempts: 3,
        base_delay: Duration::from_millis(1),
        max_delay: Duration::from_millis(4),
    }
}

/// A step that fails until its `succeed_on`th call.
async fn flaky(runs: &Counter, succeed_on: usize) -> Result<usize, BoxError> {
    let run = runs.bump();
    if run < succeed_on {
        return Err(format!("attempt {run} failed").into());
    }
    Ok(run)
}

async fn slow() -> Result<u8, BoxError> {
    tokio::time::sleep(Duration::from_secs(30)).await;
    Ok(1)
}

#[tokio::test]
async fn a_retried_step_reruns_and_is_journaled_once() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let recorder = Recorder::default();
    let options_ = StepOptions {
        retry: Some(retry()),
        ..StepOptions::default()
    };
    let value = run_workflow(options(&store, "w1", &recorder), |wf| async move {
        wf.step_with("flaky", &options_, || flaky(&runs, 3)).await
    })
    .await;
    assert_eq!(value.unwrap(), 3);
    assert_eq!(
        recorder.kinds(),
        [WorkflowKind::StepStart, WorkflowKind::StepEnd]
    );
    assert_eq!(stored(&store, "w1").await.records.len(), 1);
}

#[tokio::test]
async fn an_exhausted_retry_journals_nothing_and_reruns_on_resume() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |succeed_on: usize| {
        let runs = runs.clone();
        move |wf: WorkflowContext| async move {
            let options = StepOptions {
                retry: Some(retry()),
                ..StepOptions::default()
            };
            wf.step_with("flaky", &options, || flaky(&runs, succeed_on))
                .await
        }
    };
    let failed = run_workflow(options(&store, "w1", &Recorder::default()), flow(10)).await;
    let error = failed.unwrap_err();
    assert_eq!(error.to_string(), "step flaky failed: attempt 3 failed");
    assert_eq!(runs.get(), 3);
    assert!(stored(&store, "w1").await.records.is_empty());

    let resumed = run_workflow(options(&store, "w1", &Recorder::default()), flow(4)).await;
    assert_eq!(resumed.unwrap(), 4);
}

#[tokio::test]
async fn an_encode_error_is_never_retried() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let counted = runs.clone();
    let options_ = StepOptions {
        retry: Some(retry()),
        ..StepOptions::default()
    };
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let nan = wf.step_with("nan", &options_, || {
                counted.bump();
                std::future::ready(Ok::<_, BoxError>(f64::NAN))
            });
            nan.await
        },
    )
    .await;
    assert!(matches!(result, Err(WorkflowError::Encode { .. })));
    assert_eq!(runs.get(), 1);
}

#[tokio::test]
async fn a_step_past_its_timeout_fails_each_attempt() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let counted = runs.clone();
    let options_ = StepOptions {
        timeout: Some(Duration::from_millis(10)),
        retry: Some(Retry {
            max_attempts: 2,
            ..retry()
        }),
        ..StepOptions::default()
    };
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let step = wf.step_with("slow", &options_, || {
                counted.bump();
                slow()
            });
            step.await
        },
    )
    .await;
    let error = result.unwrap_err();
    assert_eq!(error.to_string(), "step slow timed out after 10ms");
    assert_eq!(runs.get(), 2, "a timeout is retried");
    assert_eq!(stored(&store, "w1").await.status, WorkflowStatus::Failed);
}

#[tokio::test]
async fn the_default_step_timeout_applies_unless_a_step_opts_out() {
    let store = Arc::new(MemoryJournalStore::default());
    let mut options_ = options(&store, "w1", &Recorder::default());
    options_.step_timeout = Some(Duration::from_millis(10));
    let quick = || async {
        tokio::time::sleep(Duration::from_millis(30)).await;
        Ok::<_, BoxError>(2)
    };
    let opted_out = StepOptions {
        timeout: Some(Duration::ZERO),
        ..StepOptions::default()
    };
    let value = run_workflow(options_.clone(), |wf| async move {
        let free = wf.step_with("free", &opted_out, quick).await?;
        let capped = wf.step("capped", quick).await;
        assert!(matches!(capped, Err(WorkflowError::Timeout { .. })));
        Ok(free)
    })
    .await;
    assert_eq!(value.unwrap(), 2);
}

#[tokio::test]
async fn the_deadline_fails_the_workflow_and_keeps_its_journal() {
    let store = Arc::new(MemoryJournalStore::default());
    let mut timed = options(&store, "w1", &Recorder::default());
    timed.deadline = Some(Duration::from_millis(20));
    let runs = Counter::default();
    let flow = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move {
            let first = wf.step("first", || flaky(&runs, 0)).await?;
            let second = wf.step("second", slow).await?;
            Ok(first + usize::from(second))
        }
    };
    let error = run_workflow(timed, flow).await.unwrap_err();
    assert_eq!(error.to_string(), "workflow timed out after 20ms");
    let run = stored(&store, "w1").await;
    assert_eq!((run.status, run.records.len()), (WorkflowStatus::Failed, 1));

    let recorder = Recorder::default();
    let mut resumed = options(&store, "w1", &recorder);
    resumed.step_timeout = Some(Duration::from_millis(5));
    let error = run_workflow(resumed, flow).await.unwrap_err();
    assert!(matches!(error, WorkflowError::Timeout { .. }));
    assert_eq!(runs.get(), 1);
    assert_eq!(recorder.kinds()[0], WorkflowKind::StepReplayed);
}

#[tokio::test]
async fn a_cancelled_workflow_replays_but_starts_no_new_step() {
    let store = Arc::new(MemoryJournalStore::default());
    let runs = Counter::default();
    let flow = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move {
            let first = wf.step("first", || flaky(&runs, 0)).await?;
            let second = wf.step("second", || flaky(&runs, 0)).await?;
            Ok(first + second)
        }
    };
    let once = |wf: WorkflowContext| {
        let runs = runs.clone();
        async move { wf.step("first", || flaky(&runs, 0)).await }
    };
    run_workflow(options(&store, "w1", &Recorder::default()), once)
        .await
        .unwrap();

    let cancelled = options(&store, "w1", &Recorder::default());
    cancelled.cancel.cancel();
    let error = run_workflow(cancelled, flow).await.unwrap_err();
    assert_eq!(error.to_string(), "step second was aborted");
    assert_eq!(runs.get(), 1);
    assert_eq!(stored(&store, "w1").await.status, WorkflowStatus::Failed);
}
