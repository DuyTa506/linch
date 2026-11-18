//! A tool whose start is durable and whose end is lost to a crash is never
//! run again (at most once). The model gets an error result for its
//! `tool_use_id` that says the effect is unknown, in the session so the
//! transcript stays paired; the journal marks it with a `tool_interrupted`
//! event and the error code `tool_interrupted` on the stand-in end, so a host
//! can reconcile.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    ContentBlock, Event, Message, PermissionMode, RunId, RunOptions, RunStore, ToolScope,
};

fn turns() -> Vec<linch_core::ScriptedTurn> {
    vec![tool_turn(0, 1, "Write"), text_turn("done")]
}

fn tools(runs: &Runs) -> Arc<linch_core::ToolRegistry> {
    registry(vec![Echo::new("Write", ToolScope::Write, runs)])
}

/// The error results of the session, by call id.
fn error_results(session: &[Message]) -> Vec<(String, String)> {
    let blocks = session.iter().flat_map(|message| &message.content);
    blocks
        .filter_map(|block| match block {
            ContentBlock::ToolResult(result) if result.is_error => {
                let value = serde_json::to_value(result).unwrap();
                let text = value["content"][0]["text"].as_str().unwrap_or("");
                Some((result.tool_use_id.clone(), text.to_owned()))
            }
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn a_tool_whose_end_was_lost_is_reported_unknown_and_not_run_again() {
    let clean = Arc::new(Stores::default());
    let baseline = agent(
        Arc::new(Replay::new(turns())),
        tools(&Runs::default()),
        policy(PermissionMode::SkipDangerous),
        clean.clone(),
    );
    finish(baseline.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    let mut interrupted = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let runs = Runs::default();
            let provider = Arc::new(Replay::new(turns()));
            let permissions = || policy(PermissionMode::SkipDangerous);
            let crashing = agent(provider.clone(), tools(&runs), permissions(), faulty);
            let stream = crashing.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}: the run stops");
            let Ok(record) = stores.runs.load(NS, &run_id).await else {
                continue;
            };
            if !record.status.is_terminal() {
                let again = agent(provider, tools(&runs), permissions(), stores.clone());
                finish(again.resume(run_id.clone(), RunOptions::new("s1"))).await;
            }
            let session = messages(&*stores, "s1").await;
            let placeholders = error_results(&session);
            let ends = tool_ends(&stores, &run_id).await;
            let events = interrupted_events(&stores, &run_id).await;
            let ids: Vec<&String> = placeholders.iter().map(|(id, _)| id).collect();
            assert_eq!(events.iter().collect::<Vec<_>>(), ids, "{context}: events");
            let marked: Vec<_> = ends.iter().filter(|(_, code)| code.is_some()).collect();
            assert_eq!(
                placeholders.len(),
                marked.len(),
                "{context}: {placeholders:?} against {marked:?}"
            );
            for (id, text) in &placeholders {
                interrupted += 1;
                assert!(text.contains("effect is unknown"), "{context}: {text}");
                let end = ends.iter().find(|(end_id, _)| end_id == id);
                let code = end.and_then(|(_, code)| code.as_deref());
                assert_eq!(code, Some("tool_interrupted"), "{context}: {id}");
                assert!(
                    runs.lock().unwrap().get(id).copied().unwrap_or(0) <= 1,
                    "{context}: {id} ran again"
                );
            }
        }
    }
    assert!(
        interrupted > 0,
        "a crash left a started call without an end"
    );
}

/// The `tool_use_id` of every `tool_interrupted` event of the run, in order.
async fn interrupted_events(stores: &Stores, run_id: &RunId) -> Vec<String> {
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    (events.into_iter())
        .filter_map(|envelope| match envelope.event {
            Event::ToolInterrupted(event) => Some(event.tool_use_id),
            _ => None,
        })
        .collect()
}

/// Every `tool_call_end` of the run: the call id and its error code.
async fn tool_ends(stores: &Stores, run_id: &RunId) -> Vec<(String, Option<String>)> {
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    (events.into_iter())
        .filter_map(|envelope| match envelope.event {
            Event::ToolCallEnd(end) => Some((end.tool_use_id().to_owned(), end.error_code)),
            _ => None,
        })
        .collect()
}
