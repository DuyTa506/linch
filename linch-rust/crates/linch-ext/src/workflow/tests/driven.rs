//! What a host that drives the calls itself needs (the RPC server is one):
//! a held session, begin and complete halves of a step, budgets, caps and
//! the live gate.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::json;

use super::*;
use linch_core::provider::scripted::TurnBuilder;

use crate::test_support::{
    MODEL, scripted, scripted_priced, scripted_priced_for_model_only, text_turn, usage,
};

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

/// An unpriced agent adds 0 USD per call, so a workflow USD cap could never
/// trip: the call is refused before it runs, naming the model.
#[tokio::test]
async fn a_cost_cap_refuses_a_call_of_an_unpriced_agent() {
    let fixture = scripted(vec![text_turn("a")]);
    let store = Arc::new(MemoryJournalStore::default());
    let mut capped = options(&store, "w1", &Recorder::default());
    capped.budget = Some(WorkflowBudget {
        max_tokens: None,
        max_cost_usd: Some(1.0),
    });
    let failed = run_workflow(capped, |wf| {
        let agent = fixture.agent.clone();
        async move { wf.agent(&agent, "one", "p1").await }
    })
    .await;
    let Err(WorkflowError::BudgetUnpriced { model }) = failed else {
        panic!("an unpriced agent ran under a cost cap: {failed:?}");
    };
    assert_eq!(model, MODEL);
    assert!(fixture.provider.requests().is_empty(), "no call was made");
    // A token cap alone does not need a price.
    let mut tokens = options(&store, "w2", &Recorder::default());
    tokens.budget = Some(WorkflowBudget {
        max_tokens: Some(1000),
        max_cost_usd: None,
    });
    let ran = run_workflow(tokens, |wf| {
        let agent = fixture.agent.clone();
        async move { wf.agent(&agent, "one", "p1").await }
    })
    .await;
    assert_eq!(ran.unwrap(), "a");
}

/// A fallback model with no price can serve a call too.
#[tokio::test]
async fn a_cost_cap_refuses_an_agent_with_an_unpriced_fallback() {
    let fixture = scripted_priced_for_model_only(vec![text_turn("a")], &["other-model"]);
    assert_eq!(fixture.agent.unpriced_model(), Some("other-model"));
    let store = Arc::new(MemoryJournalStore::default());
    let mut capped = options(&store, "w1", &Recorder::default());
    capped.budget = Some(WorkflowBudget {
        max_tokens: None,
        max_cost_usd: Some(1.0),
    });
    let failed = run_workflow(capped, |wf| {
        let agent = fixture.agent.clone();
        async move { wf.agent(&agent, "one", "p1").await }
    })
    .await;
    let Err(WorkflowError::BudgetUnpriced { model }) = failed else {
        panic!("an unpriced fallback ran under a cost cap: {failed:?}");
    };
    assert_eq!(model, "other-model");
    assert!(fixture.provider.requests().is_empty(), "no call was made");
}

/// The agent's price lookup passed, but a model with no rate served the
/// call: its cost is unknown, so the cap cannot count it and the next call
/// is refused, on a resume too (which replays the journal).
#[tokio::test]
async fn a_cost_cap_ends_the_workflow_after_a_call_with_no_price() {
    let served = TurnBuilder::new("served-model")
        .text("a")
        .usage(usage(10, 5))
        .build();
    let fixture = scripted_priced_for_model_only(vec![served, text_turn("b")], &[]);
    let store = Arc::new(MemoryJournalStore::default());
    let budget = WorkflowBudget {
        max_tokens: None,
        max_cost_usd: Some(1.0),
    };
    let flow = |wf: WorkflowContext| {
        let agent = fixture.agent.clone();
        async move {
            let first = wf.agent(&agent, "one", "p1").await?;
            let second = wf.agent(&agent, "two", "p2").await?;
            Ok(first + &second)
        }
    };
    for _ in 0..2 {
        let mut run = options(&store, "w1", &Recorder::default());
        run.budget = Some(budget);
        let failed = run_workflow(run, flow).await;
        let Err(WorkflowError::BudgetUnpriced { model }) = failed else {
            panic!("a call with no price passed a cost cap: {failed:?}");
        };
        assert_eq!(model, "served-model");
        assert_eq!(fixture.provider.requests().len(), 1, "the second never ran");
    }
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
async fn calls_that_journal_nothing_are_counted_against_max_call_keys_not_max_records() {
    let store = Arc::new(MemoryJournalStore::default());
    let mut capped = options(&store, "w1", &Recorder::default());
    capped.max_records = Some(1);
    capped.max_call_keys = Some(3);
    let session = WorkflowSession::open(&capped).await.unwrap();
    let wf = session.context();
    for number in 0..3 {
        let asked = wf
            .interrupt::<serde_json::Value>(&format!("k{number}"), json!({}))
            .await;
        assert!(
            matches!(asked, Err(WorkflowError::Suspended(_))),
            "{asked:?}"
        );
    }
    let refused = wf.interrupt::<serde_json::Value>("k3", json!({})).await;
    assert!(
        matches!(
            &refused,
            Err(WorkflowError::LimitExceeded {
                what: "call keys",
                max: 3
            })
        ),
        "{refused:?}"
    );
    // A key already used is not a new one.
    let again = wf.interrupt::<serde_json::Value>("k0", json!({})).await;
    assert!(
        matches!(again, Err(WorkflowError::Suspended(_))),
        "{again:?}"
    );
}

#[tokio::test]
async fn a_replay_is_never_refused_by_max_call_keys() {
    let store = Arc::new(MemoryJournalStore::default());
    let first = options(&store, "w1", &Recorder::default());
    run_workflow(first, |wf: WorkflowContext| async move {
        for number in 0..5 {
            wf.step(&format!("s{number}"), || async {
                Ok::<_, std::io::Error>(number)
            })
            .await?;
        }
        Ok(())
    })
    .await
    .unwrap();
    let mut again = options(&store, "w1", &Recorder::default());
    again.max_call_keys = Some(2);
    let replayed = run_workflow(again, |wf: WorkflowContext| async move {
        let mut sum = 0;
        for number in 0..5 {
            sum += wf
                .step(&format!("s{number}"), || async {
                    Ok::<_, std::io::Error>(99)
                })
                .await?;
        }
        // A new key past the cap is refused.
        let new = wf
            .step("new", || async { Ok::<_, std::io::Error>(1) })
            .await;
        assert!(
            matches!(
                new,
                Err(WorkflowError::LimitExceeded {
                    what: "call keys",
                    ..
                })
            ),
            "{new:?}"
        );
        Ok(sum)
    })
    .await
    .unwrap();
    assert_eq!(replayed, 0 + 1 + 2 + 3 + 4);
}

#[tokio::test]
async fn a_workflow_with_no_cap_keeps_every_pending_step() {
    let store = Arc::new(MemoryJournalStore::default());
    let session = WorkflowSession::open(&options(&store, "w1", &Recorder::default()))
        .await
        .unwrap();
    let wf = session.context();
    let tickets: Vec<_> = (0..50)
        .map(|number| wf.step_begin(&format!("s{number}"), None).unwrap())
        .collect();
    assert_eq!(tickets.len(), 50);
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
