//! What a host that drives the calls itself needs (the RPC server is one):
//! a held session, begin and complete halves of a step, budgets, caps and
//! the live gate.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::json;

use super::*;
use crate::test_support::{scripted, scripted_priced, text_turn};

#[tokio::test]
async fn a_held_workflow_refuses_a_second_driver_and_leaves_the_first_writing() {
    let store = Arc::new(MemoryJournalStore::default());
    let first = WorkflowSession::open_held(&options(&store, "w1", &Recorder::default()))
        .await
        .unwrap();
    let second = WorkflowSession::open_held(&options(&store, "w1", &Recorder::default())).await;
    assert!(
        matches!(
            second,
            Err(WorkflowError::Store(JournalStoreError::Held { .. }))
        ),
        "{:?}",
        second.err()
    );
    // The refusal claimed nothing, so the first driver is not fenced out.
    assert_eq!(stored(&store, "w1").await.epoch, 1);
    let slot = first.context().step_begin("s", None).unwrap();
    let StepSlot::Run(ticket) = slot else {
        panic!("a new step has no record")
    };
    first
        .context()
        .step_complete(ticket, json!(1))
        .await
        .unwrap();
    first.finish(true).await.unwrap();
    assert_eq!(stored(&store, "w1").await.records.len(), 1);

    drop(first);
    let again = WorkflowSession::open_held(&options(&store, "w1", &Recorder::default())).await;
    assert!(again.is_ok(), "a dropped session frees the workflow");
}

#[tokio::test]
async fn a_step_begun_and_completed_replays_its_value() {
    let store = Arc::new(MemoryJournalStore::default());
    let recorder = Recorder::default();
    let first = WorkflowSession::open(&options(&store, "w1", &recorder))
        .await
        .unwrap();
    let key = json!("doc-7");
    let StepSlot::Run(ticket) = first.context().step_begin("sign", Some(&key)).unwrap() else {
        panic!("a new step has no record")
    };
    first
        .context()
        .step_complete(ticket, json!({"sig": "abc"}))
        .await
        .unwrap();
    let kinds = [WorkflowKind::StepStart, WorkflowKind::StepEnd];
    assert_eq!(recorder.kinds(), kinds);

    let recorder = Recorder::default();
    let second = WorkflowSession::open(&options(&store, "w1", &recorder))
        .await
        .unwrap();
    assert_eq!(second.stored(), 1);
    let slot = second.context().step_begin("sign", Some(&key)).unwrap();
    assert!(matches!(slot, StepSlot::Replayed(value) if value == json!({"sig": "abc"})));
    assert_eq!(recorder.kinds(), [WorkflowKind::StepReplayed]);
    // Another input is another step.
    let other = second.context().step_begin("sign", Some(&json!("doc-8")));
    assert!(matches!(other.unwrap(), StepSlot::Run(_)));
}

#[tokio::test]
async fn a_branch_path_counts_its_steps_apart() {
    let store = Arc::new(MemoryJournalStore::default());
    let session = WorkflowSession::open(&options(&store, "w1", &Recorder::default()))
        .await
        .unwrap();
    let wf = session.context().clone();
    for path in ["0", "1"] {
        let begun = wf.scoped(path, async { wf.step_begin("s", None) }).await;
        let StepSlot::Run(ticket) = begun.unwrap() else {
            panic!("new")
        };
        wf.step_complete(ticket, json!(path)).await.unwrap();
    }
    let keys: Vec<_> = stored(&store, "w1")
        .await
        .records
        .into_iter()
        .map(|record| {
            (
                record.call_key.rsplit('@').next().unwrap().to_owned(),
                record.occurrence,
            )
        })
        .collect();
    assert_eq!(keys, [("0".to_owned(), 0), ("1".to_owned(), 0)]);
}

#[tokio::test]
async fn an_edited_prompt_misses_only_its_own_call() {
    let fixture = scripted(vec![text_turn("a"), text_turn("b"), text_turn("c")]);
    let store = Arc::new(MemoryJournalStore::default());
    let flow = |first: &'static str| {
        let agent = fixture.agent.clone();
        move |wf: WorkflowContext| async move {
            let one = wf.agent(&agent, "step", first).await?;
            let two = wf.agent(&agent, "step", "second prompt").await?;
            Ok(format!("{one}{two}"))
        }
    };
    let ran = run_workflow(options(&store, "w1", &Recorder::default()), flow("first")).await;
    assert_eq!(ran.unwrap(), "ab");
    assert_eq!(fixture.provider.requests().len(), 2);
    let recorder = Recorder::default();
    let edited = run_workflow(options(&store, "w1", &recorder), flow("first, edited")).await;
    assert_eq!(edited.unwrap(), "cb");
    assert_eq!(fixture.provider.requests().len(), 3, "one new call");
    assert_eq!(
        recorder.kinds(),
        [
            WorkflowKind::AgentStart,
            WorkflowKind::AgentEnd,
            WorkflowKind::AgentReplayed
        ]
    );
}

#[tokio::test]
async fn the_budget_counts_replayed_calls_and_stops_the_next_live_one() {
    let fixture = scripted_priced(vec![text_turn("a"), text_turn("b")]);
    let store = Arc::new(MemoryJournalStore::default());
    let budget = WorkflowBudget {
        max_tokens: Some(15),
        max_cost_usd: None,
    };
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move {
            wf.agent(&agent, "one", "p1").await?;
            wf.agent(&agent, "two", "p2").await
        }
    };
    let mut first = options(&store, "w1", &Recorder::default());
    first.budget = Some(budget);
    let failed = run_workflow(first, flow).await;
    assert!(
        matches!(
            failed,
            Err(WorkflowError::BudgetExceeded {
                spent_tokens: 15,
                ..
            })
        ),
        "{failed:?}"
    );
    assert_eq!(fixture.provider.requests().len(), 1, "the second never ran");
    // A resume starts from what the journal says was spent, not from zero.
    let mut again = options(&store, "w1", &Recorder::default());
    again.budget = Some(budget);
    let failed = run_workflow(again, flow).await;
    assert!(matches!(failed, Err(WorkflowError::BudgetExceeded { .. })));
    assert_eq!(fixture.provider.requests().len(), 1);
}

#[tokio::test]
async fn caps_on_records_and_their_size_refuse_before_a_call_spends() {
    let fixture = scripted(vec![text_turn("a"), text_turn("b")]);
    let store = Arc::new(MemoryJournalStore::default());
    let mut capped = options(&store, "w1", &Recorder::default());
    capped.max_records = Some(1);
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move {
            wf.agent(&agent, "one", "p1").await?;
            wf.agent(&agent, "two", "p2").await
        }
    };
    let failed = run_workflow(capped, flow).await;
    assert!(
        matches!(failed, Err(WorkflowError::LimitExceeded { max: 1, .. })),
        "{failed:?}"
    );
    assert_eq!(fixture.provider.requests().len(), 1, "no call past the cap");

    let mut small = options(&store, "w2", &Recorder::default());
    small.max_record_bytes = Some(8);
    let big = run_workflow(small, |wf: WorkflowContext| async move {
        wf.step("big", || async { Ok::<_, std::io::Error>("x".repeat(100)) })
            .await
    })
    .await;
    assert!(matches!(big, Err(WorkflowError::LimitExceeded { .. })));
    assert!(stored(&store, "w2").await.records.is_empty());
}

#[tokio::test]
async fn the_live_gate_is_asked_only_for_calls_that_run() {
    let fixture = scripted(vec![text_turn("a")]);
    let store = Arc::new(MemoryJournalStore::default());
    let asked = Arc::new(AtomicUsize::new(0));
    let gate_asked = asked.clone();
    let gate: LiveGate = Arc::new(move || match gate_asked.fetch_add(1, Ordering::SeqCst) {
        0 => Ok(Box::new(())),
        _ => Err(WorkflowError::LimitExceeded {
            what: "live calls",
            max: 1,
        }),
    });
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move { wf.agent(&agent, "one", "p1").await }
    };
    let mut first = options(&store, "w1", &Recorder::default());
    first.live_gate = Some(gate.clone());
    run_workflow(first, flow).await.unwrap();
    let mut replay = options(&store, "w1", &Recorder::default());
    replay.live_gate = Some(gate.clone());
    run_workflow(replay, flow).await.unwrap();
    assert_eq!(asked.load(Ordering::SeqCst), 1, "a replay is not live work");
    let mut refused = options(&store, "w2", &Recorder::default());
    refused.live_gate = Some(gate);
    let failed = run_workflow(refused, flow).await;
    assert!(matches!(failed, Err(WorkflowError::LimitExceeded { .. })));
    assert_eq!(fixture.provider.requests().len(), 1);
}

#[tokio::test]
async fn a_forked_call_continues_the_conversation_of_an_earlier_call() {
    let fixture = scripted(vec![text_turn("a"), text_turn("b")]);
    let store = Arc::new(MemoryJournalStore::default());
    let session = WorkflowSession::open(&options(&store, "w1", &Recorder::default()))
        .await
        .unwrap();
    let wf = session.context();
    let first = wf
        .agent_with(
            &fixture.agent,
            AgentCall::new("one", "remember the number 7"),
        )
        .await
        .unwrap();
    let mut forked = AgentCall::new("two", "what was the number?");
    forked.fork = Some(first.session_id.clone());
    let second = wf.agent_with(&fixture.agent, forked).await.unwrap();
    assert_ne!(first.session_id, second.session_id);

    let requests = fixture.provider.requests();
    let seen = format!("{:?}", requests[1].messages);
    assert!(
        seen.contains("remember the number 7"),
        "the fork has the prefix"
    );
    assert!(seen.contains("what was the number?"));
}
