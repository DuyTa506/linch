//! The provider-failure terminal (P4-D open question 4): the failed receipt,
//! the `Error`, the status and the `Result` go in one commit, so a crash at
//! any write leaves no failed receipt under a running run.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::{ScriptedError, TurnBuilder};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, Event, PermissionMode, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider,
    ScriptedTurn, Usage,
};

/// Calls that report usage, then break: failed receipts that were paid.
fn failing_turns() -> Vec<ScriptedTurn> {
    let failing = || {
        let mut turn = TurnBuilder::new(MODEL)
            .text("partial")
            .usage(usage(7, 3))
            .build();
        turn.error = Some(ScriptedError::Transport {
            message: "reset".into(),
        });
        turn
    };
    vec![failing(), failing()]
}

fn failing_agent<S: linch_core::SessionStore + RunStore + 'static>(stores: Arc<S>) -> Agent {
    let provider = Arc::new(ScriptedProvider::new(failing_turns()));
    let permissions = policy(PermissionMode::SkipDangerous);
    agent(provider, registry(vec![]), permissions, stores)
}

#[tokio::test]
async fn a_provider_failure_ends_the_run_in_one_write() {
    let stores = Arc::new(Stores::default());
    finish(failing_agent(stores.clone()).run("go", RunOptions::new("s1"))).await;
    let writes = stores.log.lock().unwrap().clone();
    let terminal = ["model_call_receipt", "error", "result"].map(String::from);
    assert_eq!(writes.last(), Some(&Write::Commit(terminal.to_vec())));
}

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
        }
        Err(_) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
    }
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_failing_run_keeps_its_usage() {
    let clean = Arc::new(Stores::default());
    finish(failing_agent(clean.clone()).run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = failing_agent(faulty).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = failing_agent(stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Failed, "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let receipts: Vec<_> = (events.iter())
                .filter_map(|e| match &e.event {
                    Event::ModelCallReceipt(receipt) => Some(receipt),
                    _ => None,
                })
                .collect();
            let deliveries = (events.iter())
                .filter(|e| matches!(e.event, Event::ModelCallDelivery(_)))
                .count();
            assert_eq!(receipts.len(), deliveries, "{context}: one receipt each");
            let paid = (receipts.iter())
                .filter_map(|receipt| receipt.usage)
                .fold(Usage::default(), |total, usage| total + usage);
            let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
                panic!("{context}: the run has no result");
            };
            assert_eq!(result.total_usage, paid, "{context}: every receipt counts");
            let complete = receipts.iter().all(|receipt| receipt.usage.is_some());
            assert_eq!(result.usage_complete, complete, "{context}");
        }
    }
}
