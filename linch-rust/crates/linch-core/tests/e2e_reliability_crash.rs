//! A crash at every write of a run that retries and fails over: the
//! resumed run settles every delivery once, keeps one accepted receipt per
//! turn (no answer is bought twice once it was durable), fails over once,
//! stays on the fallback, and its result counts every receipt.

mod common;

use std::sync::Arc;

use common::flaky::Flaky;
use common::*;
use linch_core::events::model_call::CallDisposition;
use linch_core::provider::scripted::{ScriptedError, TurnBuilder};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentSpec, Envelope, Event, PermissionMode, ProviderRetry, RunId, RunOptions, RunStatus,
    RunStore, SessionStore, ToolScope, Usage,
};

const FALLBACK: &str = "fallback-a";

fn spec_() -> AgentSpec {
    let mut spec = spec();
    spec.fallback_models = vec![FALLBACK.into()];
    spec.provider_retry = ProviderRetry {
        max_attempts: 2,
        base_delay_ms: 1,
        max_delay_ms: 1_000,
        jitter: 0.0,
    };
    spec
}

/// The spec's model is down; the fallback answers a tool call, then
/// breaks once after reporting its usage (a paid failure) and answers.
fn provider() -> Arc<Flaky> {
    let turns = vec![tool_turn(0, 1, "Read"), text_turn("done")];
    let mut paid = TurnBuilder::new(FALLBACK)
        .text("partial")
        .usage(usage(7, 3))
        .build();
    paid.error = Some(ScriptedError::Transport {
        message: "reset".into(),
    });
    Arc::new(Flaky::new(turns).down(MODEL).fail(1, FALLBACK, paid))
}

fn agent_on<S: RunStore + SessionStore + 'static>(
    provider: Arc<Flaky>,
    stores: Arc<S>,
    runs: &Runs,
) -> Agent {
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    agent_with(spec_(), provider, tools, permissions, stores)
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

fn count(events: &[Envelope], kind: &str) -> usize {
    events
        .iter()
        .filter(|e| common::kind(&e.event) == kind)
        .count()
}

#[tokio::test]
async fn a_clean_run_retries_then_fails_over_in_its_own_writes() {
    let stores = Arc::new(Stores::default());
    let agent = agent_on(provider(), stores.clone(), &Runs::default());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let writes = stores.log.lock().unwrap().clone();
    let commit = |kinds: &[&str]| Write::Commit(kinds.iter().map(|k| k.to_string()).collect());
    let retry = commit(&["model_call_receipt", "provider_retry"]);
    let paid_retry = commit(&["model_call_receipt", "usage", "provider_retry"]);
    let failover = commit(&[
        "model_call_receipt",
        "model_fallback",
        "context_build",
        "model_call_delivery",
    ]);
    let times = |write: &Write| writes.iter().filter(|w| *w == write).count();
    assert_eq!(
        (times(&retry), times(&failover), times(&paid_retry)),
        (1, 1, 1),
        "{writes:?}"
    );
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_retried_and_failed_over_run_pays_each_answer_once() {
    let clean = Arc::new(Stores::default());
    let agent = agent_on(provider(), clean.clone(), &Runs::default());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    assert_eq!(want.len(), 4, "{want:?}");
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) =
                (Arc::new(Stores::default()), Runs::default(), provider());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = agent_on(provider.clone(), faulty, &runs).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = agent_on(provider.clone(), stores.clone(), &runs);
            let run_id = recover(&agent, &stores, run_id).await;

            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            // A crash right after the tool started leaves its interrupted
            // placeholder in place of its result; the rest is the same.
            let got = messages(&*stores, "s1").await;
            assert_eq!(got.len(), want.len(), "{context}: {got:?}");
            for at in [0, 1, 3] {
                assert_eq!(got[at], want[at], "{context}: message {at}");
            }
            let results = result_texts(&got[2]);
            let interrupted = linch_core::journal::INTERRUPTED_TOOL_TEXT.to_owned();
            assert!(
                results == result_texts(&want[2]) || results == [interrupted],
                "{context}: {results:?}"
            );
            assert!(
                runs.lock().unwrap().values().all(|&n| n <= 1),
                "{context}: a tool ran twice"
            );
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let receipts: Vec<_> = (events.iter())
                .filter_map(|e| match &e.event {
                    Event::ModelCallReceipt(receipt) => Some(receipt),
                    _ => None,
                })
                .collect();
            let deliveries = count(&events, "model_call_delivery");
            assert_eq!(receipts.len(), deliveries, "{context}: one receipt each");
            let accepted: Vec<_> = (receipts.iter())
                .filter(|r| r.disposition == CallDisposition::Accepted)
                .collect();
            assert_eq!(accepted.len(), 2, "{context}: one answer per turn");
            assert!(
                (accepted.iter()).all(|r| r.actual_model.as_deref() == Some(FALLBACK)),
                "{context}: the fallback answered"
            );
            assert_eq!(count(&events, "model_fallback"), 1, "{context}");
            let fell_back = (events.iter())
                .position(|e| matches!(e.event, Event::ModelFallback(_)))
                .unwrap();
            let primary_after = events[fell_back..].iter().any(
                |e| matches!(&e.event, Event::ModelCallDelivery(d) if d.requested_model == MODEL),
            );
            assert!(!primary_after, "{context}: the run went back to its model");
            let paid = (receipts.iter())
                .filter_map(|receipt| receipt.usage)
                .fold(Usage::default(), |total, usage| total + usage);
            let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
                panic!("{context}: the run has no result");
            };
            assert_eq!(result.total_usage, paid, "{context}: every receipt counts");
            let complete = receipts.iter().all(|receipt| receipt.usage.is_some());
            assert_eq!(result.usage_complete, complete, "{context}");
            let last = (events.iter().rev()).find_map(|e| match &e.event {
                Event::Usage(usage) => usage.actual_model.clone(),
                _ => None,
            });
            assert_eq!(last.as_deref(), Some(FALLBACK), "{context}");
        }
    }
}
