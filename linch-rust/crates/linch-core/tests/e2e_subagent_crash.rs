//! A crash at any write while a subagent runs, then a resume of the parent:
//! the call runs again and resumes its child run (or reads the result of
//! one that finished), so the parent gets the child's real answer and no
//! paid model call is made twice, in the parent or the child.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::subagents::*;
use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::events::model_call::CallDisposition;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, PermissionMode, RunId, RunOptions, RunStatus, RunStore,
    SessionStore, StoreError, ToolScope,
};

fn family() -> Arc<Family> {
    let parent = vec![spawn_turn(None, &["check x"]), text_turn("done")];
    let child = vec![tool_turn(0, 1, "Read"), text_turn("x holds")];
    Arc::new(Family::new(parent, child))
}

fn agent<S: RunStore + SessionStore + 'static>(
    family: &Arc<Family>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec())
        .provider(family.clone())
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap()
}

/// Resume the parent, or start it if the crash came before it existed.
async fn recover(agent: &Agent, stores: &Stores, run_id: &RunId) -> RunId {
    match stores.runs.load(NS, run_id).await {
        Ok(record) if record.status.is_terminal() => run_id.clone(),
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id.clone()
        }
        Err(StoreError::NotFound { .. }) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
        Err(error) => panic!("load failed: {error}"),
    }
}

/// Each turn of the run has one accepted receipt, and each extra call of
/// `who` was settled by an abandoned receipt.
fn paid_once(family: &Family, who: &str, events: &[Envelope], context: &str) {
    let mut accepted: HashMap<usize, u32> = HashMap::new();
    let mut abandoned: HashMap<usize, u32> = HashMap::new();
    for envelope in events {
        if let Event::ModelCallReceipt(receipt) = &envelope.event {
            let turn: usize = receipt
                .logical_call_id
                .rsplit(':')
                .next()
                .unwrap()
                .parse()
                .unwrap();
            match receipt.disposition {
                CallDisposition::Accepted => *accepted.entry(turn).or_default() += 1,
                CallDisposition::Cancelled => *abandoned.entry(turn).or_default() += 1,
                other => panic!("{context}: unexpected receipt {other:?}"),
            }
        }
    }
    let calls = family.calls.lock().unwrap().clone();
    for turn in 0..2 {
        assert_eq!(
            accepted.get(&turn),
            Some(&1),
            "{context}: {who} turn {turn}"
        );
        let made = calls.get(&(who.to_owned(), turn)).copied().unwrap_or(0);
        let spare = abandoned.get(&turn).copied().unwrap_or(0);
        assert!(
            made >= 1 && made <= 1 + spare,
            "{context}: {who} turn {turn} called {made} times"
        );
    }
}

#[tokio::test]
async fn a_crash_with_a_child_in_flight_resumes_the_child() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = agent(&family(), &Runs::default(), stores.clone());
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    assert!(writes > 20, "parent and child writes: {writes}");
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let family = family();
            let runs = Runs::default();
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let stream = agent(&family, &runs, dying).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            assert!(error.is_some(), "{context}: the parent must stop");
            let clean = agent(&family, &runs, stores.clone());
            let run_id = recover(&clean, &stores, &run_id).await;

            let parent = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(parent.status, RunStatus::Completed, "{context}");
            let log = messages(&*stores, "s1").await;
            assert_eq!(result_texts(&log[2]), ["x holds"], "{context}");
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let child = stores.runs.load(NS, &child_id).await.unwrap();
            assert_eq!(child.status, RunStatus::Completed, "{context}");
            assert_eq!(child.parent_run_id, Some(run_id.clone()), "{context}");

            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            paid_once(&family, "parent", &events, &context);
            let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
                panic!("{context}: the parent ends with its result")
            };
            assert_eq!(result.subtype, ResultSubtype::Success, "{context}");
            let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
            paid_once(&family, "check x", &child_events, &context);
            let child_log = messages(&*stores, child_id.as_str()).await;
            let read = result_texts(&child_log[2]);
            let ran = runs.lock().unwrap().get("call_0_0").copied().unwrap_or(0);
            match read[0].as_str() {
                INTERRUPTED_TOOL_TEXT => assert!(ran <= 1, "{context}: ran {ran}"),
                _ => assert_eq!(ran, 1, "{context}: the child's tool ran {ran} times"),
            }
        }
    }
}
