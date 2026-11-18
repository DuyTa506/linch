//! Human in the loop: interrupts suspend, `resume` answers them, and the
//! answer is journaled.

use std::collections::HashMap;
use std::convert::Infallible;
use std::sync::Arc;

use linch_core::events::kinds::WorkflowKind;
use serde_json::json;

use super::*;

fn answered(store: &Arc<MemoryJournalStore>, recorder: &Recorder, answer: bool) -> WorkflowOptions {
    let mut options = options(store, "w1", recorder);
    options.resume = HashMap::from([("approve".to_owned(), json!(answer))]);
    options
}

async fn approval_flow(wf: WorkflowContext) -> Result<String, WorkflowError> {
    let plan = wf
        .step("plan", || async {
            Ok::<_, Infallible>("drop table".to_owned())
        })
        .await?;
    let approved: bool = wf.interrupt("approve", json!({"plan": plan})).await?;
    if !approved {
        return Ok("cancelled".into());
    }
    wf.step("apply", || async {
        Ok::<_, Infallible>("applied".to_owned())
    })
    .await
}

#[tokio::test]
async fn an_unanswered_interrupt_suspends_the_workflow() {
    let store = Arc::new(MemoryJournalStore::default());
    let recorder = Recorder::default();
    let result = run_workflow(options(&store, "w1", &recorder), approval_flow).await;

    let interrupt = Interrupt {
        key: "approve".into(),
        payload: json!({"plan": "drop table"}),
    };
    match result {
        Err(WorkflowError::Suspended(suspended)) => assert_eq!(suspended, interrupt),
        other => panic!("expected a suspend, got {other:?}"),
    }
    let run = stored(&store, "w1").await;
    assert_eq!(run.status, WorkflowStatus::AwaitingInput(interrupt));
    assert_eq!(run.records.len(), 1);
    let requested = recorder.events().pop().unwrap();
    assert_eq!(requested.kind, WorkflowKind::InterruptRequested);
    assert_eq!(requested.call_key, interrupt_key("approve"));
    let payload = requested.structured_output.unwrap();
    assert_eq!(payload["payload"], json!({"plan": "drop table"}));
}

#[tokio::test]
async fn a_resume_answer_is_journaled_and_replays_later() {
    let store = Arc::new(MemoryJournalStore::default());
    let first = run_workflow(options(&store, "w1", &Recorder::default()), approval_flow).await;
    assert!(matches!(first, Err(WorkflowError::Suspended(_))));

    let recorder = Recorder::default();
    let resumed = run_workflow(answered(&store, &recorder, true), approval_flow).await;
    assert_eq!(resumed.unwrap(), "applied");
    assert_eq!(
        recorder.kinds(),
        [
            WorkflowKind::StepReplayed,
            WorkflowKind::InterruptResolved,
            WorkflowKind::StepStart,
            WorkflowKind::StepEnd,
        ]
    );
    assert_eq!(stored(&store, "w1").await.status, WorkflowStatus::Completed);

    let later = Recorder::default();
    let replayed = run_workflow(options(&store, "w1", &later), approval_flow).await;
    assert_eq!(replayed.unwrap(), "applied");
    assert_eq!(later.kinds()[1], WorkflowKind::InterruptReplayed);
    assert_eq!(later.events()[1].result_text.as_deref(), Some("true"));
}

#[tokio::test]
async fn a_resume_answer_is_used_once() {
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf: WorkflowContext| async move {
        let first: bool = wf.interrupt("approve", "first").await?;
        let second: bool = wf.interrupt("approve", "second").await?;
        Ok((first, second))
    };
    let asked = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    match asked {
        Err(WorkflowError::Suspended(interrupt)) => assert_eq!(interrupt.payload, "first"),
        other => panic!("expected a suspend, got {other:?}"),
    }
    let result = run_workflow(answered(&store, &Recorder::default(), true), flow).await;
    match result {
        Err(WorkflowError::Suspended(interrupt)) => assert_eq!(interrupt.payload, "second"),
        other => panic!("expected a second suspend, got {other:?}"),
    }
    let resumed = run_workflow(answered(&store, &Recorder::default(), false), flow).await;
    assert_eq!(resumed.unwrap(), (true, false));
}

#[tokio::test]
async fn a_swallowed_suspend_still_suspends_the_run() {
    let store = Arc::new(MemoryJournalStore::default());
    let result = run_workflow(
        options(&store, "w1", &Recorder::default()),
        |wf| async move {
            let ignored = wf.interrupt::<bool>("approve", json!(null)).await;
            assert!(ignored.is_err());
            Ok("carried on")
        },
    )
    .await;
    assert!(matches!(result, Err(WorkflowError::Suspended(_))));
    let status = stored(&store, "w1").await.status;
    assert!(matches!(status, WorkflowStatus::AwaitingInput(_)));
}

#[tokio::test]
async fn an_answer_of_the_wrong_type_fails_and_is_not_journaled() {
    let store = Arc::new(MemoryJournalStore::default());
    let first = run_workflow(options(&store, "w1", &Recorder::default()), approval_flow).await;
    assert!(matches!(first, Err(WorkflowError::Suspended(_))));
    let mut wrong = options(&store, "w1", &Recorder::default());
    wrong.resume = HashMap::from([("approve".to_owned(), json!("yes"))]);
    let result = run_workflow(wrong, approval_flow).await;
    let error = result.unwrap_err();
    assert!(matches!(error, WorkflowError::Encode { .. }), "{error}");
    let run = stored(&store, "w1").await;
    assert_eq!((run.status, run.records.len()), (WorkflowStatus::Failed, 1));
}

/// What the approver is shown: the interrupt's payload, here a draft that
/// the workflow rebuilds on every run.
async fn publish_flow(wf: WorkflowContext, draft: &'static str) -> Result<bool, WorkflowError> {
    wf.interrupt("approve", json!({"draft": draft})).await
}

/// An approval is for the question it answered. If the draft changed between
/// the question and the resume, the old approval must not publish the new one.
#[tokio::test]
async fn an_answer_for_an_edited_interrupt_is_not_applied() {
    let store = Arc::new(MemoryJournalStore::default());
    let first = run_workflow(options(&store, "w1", &Recorder::default()), |wf| {
        publish_flow(wf, "v1")
    })
    .await;
    assert!(matches!(first, Err(WorkflowError::Suspended(_))));

    let recorder = Recorder::default();
    let edited = answered(&store, &recorder, true);
    let resumed = run_workflow(edited, |wf| publish_flow(wf, "v2")).await;
    match resumed {
        Err(WorkflowError::Suspended(interrupt)) => {
            assert_eq!(interrupt.payload, json!({"draft": "v2"}));
        }
        other => panic!("the approval of v1 was applied to v2: {other:?}"),
    }
    assert_eq!(
        recorder.kinds(),
        [
            WorkflowKind::InterruptStale,
            WorkflowKind::InterruptRequested
        ]
    );
    let run = stored(&store, "w1").await;
    assert!(run.records.is_empty(), "the stale answer was journaled");
    let interrupt = Interrupt {
        key: "approve".into(),
        payload: json!({"draft": "v2"}),
    };
    assert_eq!(run.status, WorkflowStatus::AwaitingInput(interrupt));

    // Answered again for the question as it now stands, it applies.
    let again = answered(&store, &Recorder::default(), true);
    let published = run_workflow(again, |wf| publish_flow(wf, "v2")).await;
    assert!(published.unwrap());
}

/// An answer to a question the journal never stored is not applied either.
#[tokio::test]
async fn an_answer_for_a_question_never_asked_is_not_applied() {
    let store = Arc::new(MemoryJournalStore::default());
    let options = answered(&store, &Recorder::default(), true);
    let result = run_workflow(options, |wf| publish_flow(wf, "v1")).await;
    assert!(
        matches!(result, Err(WorkflowError::Suspended(_))),
        "{result:?}"
    );
}

/// A workflow parked on one question takes the answer of that one. Another
/// interrupt reached in the same resume asks again, even with an answer
/// given, so the second is answered on the next resume.
#[tokio::test]
async fn only_the_stored_question_takes_its_answer() {
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |wf: WorkflowContext| async move {
        let ask = |key: &'static str| {
            let wf = wf.clone();
            async move { wf.interrupt::<bool>(key, json!({"q": key})).await }
        };
        wf.settled([ask("a"), ask("b")]).await
    };
    let first = run_workflow(options(&store, "w1", &Recorder::default()), flow).await;
    assert!(matches!(first, Err(WorkflowError::Suspended(_))));
    let stored_key = match stored(&store, "w1").await.status {
        WorkflowStatus::AwaitingInput(interrupt) => interrupt.key,
        other => panic!("{other:?}"),
    };
    let mut both = options(&store, "w1", &Recorder::default());
    both.resume = HashMap::from([("a".to_owned(), json!(true)), ("b".to_owned(), json!(true))]);
    let second = run_workflow(both, flow).await;
    let Err(WorkflowError::Suspended(left)) = second else {
        panic!("one of the two answers must not apply: {second:?}");
    };
    assert_ne!(left.key, stored_key, "the stored question took its answer");
    let run = stored(&store, "w1").await;
    assert_eq!(run.records.len(), 1);
}
