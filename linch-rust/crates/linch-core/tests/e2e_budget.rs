//! Run budgets (Python's `RunBudget`): a token and USD cap charged after
//! each provider call and checked before the next one, shared by a run and
//! its subagent children, warning once, and kept across a crash and resume.

mod common;

use std::sync::Arc;

use common::subagents::{Dying, Family, spawn_turn};
use common::*;
use linch_core::events::kinds::{BudgetEvent, BudgetKind, ResultSubtype};
use linch_core::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Envelope, Event, PermissionMode, Provider,
    RunBudget, RunId, RunOptions, RunStatus, RunStore, SessionStore, StopReason, StoreError,
    ToolScope, Usage,
};

const SKIP: PermissionMode = PermissionMode::SkipDangerous;

fn capped(max_tokens: Option<u64>, max_cost_usd: Option<f64>, warn_ratio: f64) -> AgentSpec {
    let mut spec = spec();
    spec.budget = Some(RunBudget {
        max_tokens,
        max_cost_usd,
        warn_ratio,
    });
    spec
}

/// A thousandth of a USD per input token, two per output token.
fn price(_model: &str, usage: &Usage) -> Option<f64> {
    Some(usage.input_tokens as f64 * 0.001 + usage.output_tokens as f64 * 0.002)
}

fn built<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    stores: Arc<S>,
    subagents: bool,
) -> Agent {
    let runs = Runs::default();
    let builder = AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![
            Echo::new("Echo", ToolScope::Read, &runs),
            Echo::new("Read", ToolScope::Read, &runs),
        ]))
        .permissions(policy(SKIP))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .pricing(price);
    let builder = if subagents {
        builder.subagents(Vec::new())
    } else {
        builder
    };
    builder.build().unwrap()
}

fn budgets(events: &[Envelope]) -> Vec<&BudgetEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::Budget(budget) => Some(budget),
            _ => None,
        })
        .collect()
}

fn receipts(events: &[Envelope]) -> Vec<&ModelCallReceiptEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt),
            _ => None,
        })
        .collect()
}

/// The tokens of the durable receipts in `events`, all four buckets.
fn paid_tokens(events: &[Envelope]) -> u64 {
    (receipts(events).iter())
        .filter_map(|receipt| receipt.usage)
        .map(|u| u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_creation_tokens)
        .sum()
}

/// Whether `events` hold a call whose usage nobody knows.
fn unknown_use(events: &[Envelope]) -> bool {
    (receipts(events).iter())
        .any(|r| r.usage.is_none() && r.disposition != CallDisposition::NotDispatched)
}

/// The `name` and `message` of the run's error event.
fn error(events: &[Envelope]) -> Option<(String, String)> {
    events.iter().find_map(|e| match &e.event {
        Event::Error(error) => Some((
            error.error["name"].as_str()?.to_owned(),
            error.error["message"].as_str()?.to_owned(),
        )),
        _ => None,
    })
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

#[tokio::test]
async fn a_spent_cap_stops_the_run_before_its_next_call() {
    // 15 tokens a call: the second call reaches the cap of 30.
    let stores = Arc::new(Stores::default());
    let replay = Arc::new(Replay::new(script(3, 1, "Echo")));
    let agent = built(
        capped(Some(30), None, 0.9),
        replay.clone(),
        stores.clone(),
        false,
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let calls = replay.calls();
    assert_eq!(
        (calls.get(&0), calls.get(&1), calls.get(&2)),
        (Some(&1), Some(&1), None)
    );
    let tail: Vec<String> = kinds(&envelopes).into_iter().rev().take(3).collect();
    assert_eq!(tail, ["result", "error", "budget"]);
    let exceeded = budgets(&envelopes)[1];
    assert_eq!(exceeded.kind, BudgetKind::Exceeded);
    assert_eq!((exceeded.spent_tokens, exceeded.max_tokens), (30, Some(30)));
    assert_eq!(exceeded.max_cost_usd, None);
    let (name, message) = error(&envelopes).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(message, "run budget exhausted (30 tokens, $0.0400 spent)");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert_eq!(result.stop_reason, StopReason::Error);
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);

    // The terminal costs what the turn limit's does: no write is added.
    let limited = Arc::new(Stores::default());
    let mut spec = spec();
    spec.max_turns = Some(2);
    let provider = Arc::new(Replay::new(script(3, 1, "Echo")));
    finish(built(spec, provider, limited.clone(), false).run("go", RunOptions::new("s1"))).await;
    let writes = |stores: &Stores| stores.log.lock().unwrap().len();
    assert_eq!(writes(&stores), writes(&limited));
}

#[tokio::test]
async fn the_warning_fires_once_when_the_spend_reaches_its_ratio() {
    let stores = Arc::new(Stores::default());
    let replay = Arc::new(Replay::new(script(3, 1, "Echo")));
    let agent = built(capped(Some(100), None, 0.2), replay, stores.clone(), false);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let warnings = budgets(&envelopes);
    assert_eq!(warnings.len(), 1, "{:?}", kinds(&envelopes));
    assert_eq!(warnings[0].kind, BudgetKind::Warning);
    assert_eq!(
        (warnings[0].spent_tokens, warnings[0].max_tokens),
        (30, Some(100))
    );
    // It follows the usage of the call that reached 20 tokens.
    let at = (envelopes.iter()).position(|e| matches!(e.event, Event::Budget(_)));
    let usages: Vec<usize> = (envelopes.iter().enumerate())
        .filter(|(_, e)| matches!(e.event, Event::Usage(_)))
        .map(|(i, _)| i)
        .collect();
    assert_eq!(at, Some(usages[1] + 1));
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
}

#[tokio::test]
async fn a_cost_cap_counts_the_priced_cost() {
    // 0.02 USD a call: the third reaches 0.05.
    let stores = Arc::new(Stores::default());
    let replay = Arc::new(Replay::new(script(3, 1, "Echo")));
    let agent = built(capped(None, Some(0.05), 0.9), replay.clone(), stores, false);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(replay.calls().get(&3), None);
    let found = budgets(&envelopes);
    let kinds: Vec<BudgetKind> = found.iter().map(|b| b.kind).collect();
    assert_eq!(kinds, [BudgetKind::Warning, BudgetKind::Exceeded]);
    assert!(close(found[0].spent_usd, 0.06), "{}", found[0].spent_usd);
    assert_eq!(found[1].max_cost_usd, Some(0.05));
    let (name, message) = error(&envelopes).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(message, "run budget exhausted (45 tokens, $0.0600 spent)");
}

fn tree() -> Arc<Family> {
    let parent = vec![spawn_turn(None, &["check x"]), text_turn("done")];
    let child = vec![tool_turn(0, 1, "Read"), text_turn("x holds")];
    Arc::new(Family::new(parent, child))
}

#[tokio::test]
async fn a_child_s_spend_counts_against_its_parent() {
    // The parent spends 15, its child 30: the parent's next call is over.
    let stores = Arc::new(Stores::default());
    let family = tree();
    let agent = built(
        capped(Some(45), None, 0.9),
        family.clone(),
        stores.clone(),
        true,
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let calls = family.calls.lock().unwrap().clone();
    assert_eq!(calls.get(&("parent".into(), 1)), None, "{calls:?}");
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child = stores.runs.load(NS, &child_id).await.unwrap();
    assert_eq!(child.status, RunStatus::Completed);
    let (name, message) = error(&envelopes).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(message, "run budget exhausted (45 tokens, $0.0600 spent)");

    // The tree warns once: the child's last call crossed 90%.
    let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
    let warned: Vec<BudgetKind> = budgets(&child_events).iter().map(|b| b.kind).collect();
    assert_eq!(warned, [BudgetKind::Warning]);
    let parent_events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let own: Vec<BudgetKind> = budgets(&parent_events).iter().map(|b| b.kind).collect();
    assert_eq!(own, [BudgetKind::Exceeded]);
}

#[tokio::test]
async fn an_exhausted_tree_refuses_its_child_s_first_call() {
    let stores = Arc::new(Stores::default());
    let family = tree();
    let agent = built(
        capped(Some(15), None, 0.9),
        family.clone(),
        stores.clone(),
        true,
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let calls = family.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1, "only the parent's first call: {calls:?}");
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child = stores.runs.load(NS, &child_id).await.unwrap();
    assert_eq!(child.status, RunStatus::Failed);
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        [
            "Subagent encountered an error: BudgetExceededError: run budget exhausted \
          (15 tokens, $0.0200 spent)."
        ]
    );
    assert_eq!(error(&envelopes).unwrap().0, "BudgetExceededError");
}

#[tokio::test]
async fn a_budget_is_part_of_the_contract_and_must_be_finite() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(script(0, 0, "Echo")));
    let open = built(spec(), provider.clone(), stores.clone(), false);
    let capped_agent = built(
        capped(Some(10), None, 0.9),
        provider.clone(),
        stores.clone(),
        false,
    );
    assert_ne!(open.contract(), capped_agent.contract());
    let error = AgentBuilder::new(capped(None, Some(f64::NAN), 0.9))
        .provider(provider)
        .permissions(policy(SKIP))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .err();
    assert!(matches!(error, Some(BuildError::Invalid(_))), "{error:?}");
}

/// Continue whatever a crash left: resume, or run again if never created.
/// Says whether it resumed.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> (RunId, bool) {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => (run_id, false),
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            (run_id, true)
        }
        Err(StoreError::NotFound { .. }) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            (run_id, false)
        }
        Err(error) => panic!("load failed: {error}"),
    }
}

/// A crash at any write, then a resume: the resumed run starts from what
/// its durable receipts spent, so it never makes the call the cap forbids.
#[tokio::test]
async fn a_resumed_run_keeps_what_it_spent() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let replay = Arc::new(Replay::new(script(3, 1, "Echo")));
        let agent = built(capped(Some(30), None, 0.9), replay, stores.clone(), false);
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    let mut kept = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let replay = Arc::new(Replay::new(script(3, 1, "Echo")));
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let spec = capped(Some(30), None, 0.9);
            let stream =
                built(spec.clone(), replay.clone(), dying, false).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = built(spec, replay.clone(), stores.clone(), false);
            let (run_id, resumed) = recover(&agent, &stores, run_id).await;

            assert_eq!(
                replay.calls().get(&2),
                None,
                "{context}: a call past the cap"
            );
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let (name, _) = error(&events).unwrap();
            if unknown_use(&events) {
                assert_eq!(name, "BudgetAccountingError", "{context}");
                continue;
            }
            assert_eq!(name, "BudgetExceededError", "{context}");
            let exceeded = budgets(&events)
                .into_iter()
                .find(|b| b.kind == BudgetKind::Exceeded);
            assert_eq!(
                exceeded.unwrap().spent_tokens,
                paid_tokens(&events),
                "{context}"
            );
            let warnings = budgets(&events)
                .iter()
                .filter(|b| b.kind == BudgetKind::Warning)
                .count();
            assert_eq!(warnings, 1, "{context}: the warning fires once");
            kept += usize::from(resumed);
        }
    }
    assert!(
        kept > 0,
        "no crash left a resumable run that kept its spend"
    );
}

/// The same across a subagent tree: the resumed parent counts what its
/// child's durable receipts spent too.
#[tokio::test]
async fn a_resumed_tree_keeps_what_its_children_spent() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = built(capped(Some(45), None, 0.9), tree(), stores.clone(), true);
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    let mut kept = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let family = tree();
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let spec = capped(Some(45), None, 0.9);
            let stream =
                built(spec.clone(), family.clone(), dying, true).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = built(spec, family.clone(), stores.clone(), true);
            let (run_id, resumed) = recover(&agent, &stores, run_id).await;

            let calls = family.calls.lock().unwrap().clone();
            assert_eq!(
                calls.get(&("parent".into(), 1)),
                None,
                "{context}: {calls:?}"
            );
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let child = (stores.runs.events(NS, &child_id, 0).await).unwrap_or_default();
            let (name, _) = error(&events).unwrap();
            if unknown_use(&events) || unknown_use(&child) {
                assert_eq!(name, "BudgetAccountingError", "{context}");
                continue;
            }
            assert_eq!(name, "BudgetExceededError", "{context}");
            let exceeded = budgets(&events)
                .into_iter()
                .find(|b| b.kind == BudgetKind::Exceeded);
            let spent = paid_tokens(&events) + paid_tokens(&child);
            assert_eq!(exceeded.unwrap().spent_tokens, spent, "{context}");
            kept += usize::from(resumed);
        }
    }
    assert!(
        kept > 0,
        "no crash left a resumable tree that kept its spend"
    );
}
