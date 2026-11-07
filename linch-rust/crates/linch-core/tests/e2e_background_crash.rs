//! A crash at any write while a background subagent runs, then a resume of
//! the parent in a new agent: the parent picks its background child up
//! again (resuming it, or reading the end of one that finished), hears of
//! its end exactly once, and no paid model call or tool runs twice, in the
//! parent or the child.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::subagents::*;
use common::*;
use linch_core::events::model_call::CallDisposition;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, PermissionMode, RunId, RunOptions, RunStatus, RunStore,
    SessionStore, StoreError, ToolScope, Workers,
};
use serde_json::json;

fn family() -> Arc<Family> {
    let input = json!({"description": "look", "prompt": "check x", "run_in_background": true});
    let spawn = TurnBuilder::new(MODEL)
        .tool_call("sub_0", "Subagent", &input)
        .usage(usage(10, 5))
        .build();
    let parent = vec![spawn, text_turn("waiting"), text_turn("done")];
    let child = vec![tool_turn(0, 1, "Read"), text_turn("x holds")];
    Arc::new(Family::new(parent, child))
}

fn agent<S: RunStore + SessionStore + 'static>(
    family: &Arc<Family>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let workers = Workers {
        background: true,
        ..Workers::default()
    };
    AgentBuilder::new(spec())
        .provider(family.clone())
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(Vec::new())
        .workers(workers)
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

/// Each turn with a receipt has one accepted receipt, and each extra call
/// of `who` was settled by an abandoned receipt.
fn paid_once(family: &Family, who: &str, events: &[Envelope], context: &str) {
    let mut accepted: HashMap<usize, u32> = HashMap::new();
    let mut abandoned: HashMap<usize, u32> = HashMap::new();
    for envelope in events {
        if let Event::ModelCallReceipt(receipt) = &envelope.event {
            let turn = receipt.logical_call_id.rsplit(':').next().unwrap();
            let turn: usize = turn.parse().unwrap();
            match receipt.disposition {
                CallDisposition::Accepted => *accepted.entry(turn).or_default() += 1,
                CallDisposition::Cancelled => *abandoned.entry(turn).or_default() += 1,
                other => panic!("{context}: unexpected receipt {other:?}"),
            }
        }
    }
    let calls = family.calls.lock().unwrap().clone();
    for (turn, count) in &accepted {
        assert_eq!(*count, 1, "{context}: {who} turn {turn}");
        let made = calls.get(&(who.to_owned(), *turn)).copied().unwrap_or(0);
        let spare = abandoned.get(turn).copied().unwrap_or(0);
        assert!(
            made >= 1 && made <= 1 + spare,
            "{context}: {who} turn {turn} called {made} times"
        );
    }
}

#[tokio::test]
async fn a_crash_with_a_background_child_in_flight_picks_it_up_again() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = agent(&family(), &Runs::default(), stores.clone());
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    assert!(writes > 20, "parent and child writes: {writes}");
    let mut in_flight = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let (family, runs) = (family(), Runs::default());
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let stream = agent(&family, &runs, dying).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let _ = drain(stream).await;
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let parent = stores.runs.load(NS, &run_id).await;
            let child = stores.runs.load(NS, &child_id).await;
            let unfinished = !matches!(&child, Ok(c) if c.status.is_terminal());
            in_flight += usize::from(parent.is_ok_and(|p| !p.status.is_terminal()) && unfinished);
            let clean = agent(&family, &runs, stores.clone());
            let run_id = recover(&clean, &stores, &run_id).await;

            let parent = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(parent.status, RunStatus::Completed, "{context}");
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let child = stores.runs.load(NS, &child_id).await.unwrap();
            assert_eq!(child.status, RunStatus::Completed, "{context}");
            let log = messages(&*stores, "s1").await;
            let heard: usize = (log.iter())
                .map(|m| m.text().matches("<task-notification>").count())
                .sum();
            let note = format!("<task-id>{child_id}</task-id><status>completed</status>");
            let all: String = log.iter().map(|m| m.text()).collect();
            assert_eq!(heard, 1, "{context}: heard once");
            assert!(all.contains(&note), "{context}: {all}");
            assert!(all.contains("<result>x holds</result>"), "{context}");

            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            paid_once(&family, "parent", &events, &context);
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
    assert!(
        in_flight > 8,
        "crashes with the child unfinished: {in_flight}"
    );
}
