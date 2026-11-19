//! A cap a subagent definition gives its child runs: the same tokens and
//! USD a run budget counts, reached with the same `BudgetExceededError`,
//! which ends the child (not the parent), while the parent's own budget
//! still counts the child's spend. A crash and resume keep the child's spend.

mod common;

use std::sync::Arc;

use common::subagents::{Dying, Family, spawn_turn};
use common::*;
use linch_core::events::kinds::{BudgetEvent, BudgetKind, ResultSubtype};
use linch_core::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Envelope, Event, PermissionMode, Provider,
    RunBudget, RunId, RunOptions, RunStatus, RunStore, SessionStore, StoreError,
    SubagentDefinition, ToolScope, Usage,
};

const CAPPED: &str = "capped";

/// A thousandth of a USD per input token, two per output token.
fn price(_model: &str, usage: &Usage) -> Option<f64> {
    Some(usage.input_tokens as f64 * 0.001 + usage.output_tokens as f64 * 0.002)
}

fn definition(budget: Option<RunBudget>) -> SubagentDefinition {
    let mut definition = SubagentDefinition::new(CAPPED, "a capped helper", "Help.");
    definition.budget = budget;
    definition
}

fn tokens(max: u64) -> Option<RunBudget> {
    Some(RunBudget {
        max_tokens: Some(max),
        ..RunBudget::default()
    })
}

fn built<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    stores: Arc<S>,
    definition: SubagentDefinition,
) -> Agent {
    let runs = Runs::default();
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![
            Echo::new("Echo", ToolScope::Read, &runs),
            Echo::new("Read", ToolScope::Read, &runs),
        ]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .pricing(price)
        .subagents(vec![definition])
        .build()
        .unwrap()
}

fn with_budget(max_tokens: Option<u64>, max_cost_usd: Option<f64>) -> AgentSpec {
    let mut spec = spec();
    spec.budget = Some(RunBudget {
        max_tokens,
        max_cost_usd,
        ..RunBudget::default()
    });
    spec
}

/// The parent spawns a `capped` child, which reads twice then answers
/// (15 tokens a call, 0.02 USD).
fn tree() -> Arc<Family> {
    let parent = vec![spawn_turn(Some(CAPPED), &["check x"]), text_turn("done")];
    let child = vec![
        tool_turn(0, 1, "Read"),
        tool_turn(1, 1, "Read"),
        text_turn("x holds"),
    ];
    Arc::new(Family::new(parent, child))
}

fn budgets(events: &[Envelope]) -> Vec<&BudgetEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::Budget(budget) => Some(budget),
            _ => None,
        })
        .collect()
}

fn error(events: &[Envelope]) -> Option<(String, String)> {
    events.iter().find_map(|e| match &e.event {
        Event::Error(error) => Some((
            error.error["name"].as_str()?.to_owned(),
            error.error["message"].as_str()?.to_owned(),
        )),
        _ => None,
    })
}

fn paid_tokens(events: &[Envelope]) -> u64 {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(ModelCallReceiptEvent { usage: Some(u), .. }) => {
                Some(u.input_tokens + u.output_tokens)
            }
            _ => None,
        })
        .sum()
}

fn unknown_use(events: &[Envelope]) -> bool {
    events.iter().any(|e| match &e.event {
        Event::ModelCallReceipt(r) => {
            r.usage.is_none() && r.disposition != CallDisposition::NotDispatched
        }
        _ => false,
    })
}

#[tokio::test]
async fn a_capped_child_ends_at_its_cap_and_the_parent_goes_on() {
    // The child spends 15 a call: its cap of 30 refuses its third call.
    let stores = Arc::new(Stores::default());
    let family = tree();
    let agent = built(
        spec(),
        family.clone(),
        stores.clone(),
        definition(tokens(30)),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let calls = family.calls.lock().unwrap().clone();
    let child_calls = |n: usize| calls.get(&("check x".to_owned(), n)).copied();
    assert_eq!(child_calls(0), Some(1));
    assert_eq!(child_calls(1), Some(1));
    assert_eq!(child_calls(2), None, "no call past the cap: {calls:?}");
    assert_eq!(
        calls.get(&("parent".into(), 1)),
        Some(&1),
        "the parent took its next turn"
    );

    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child = stores.runs.load(NS, &child_id).await.unwrap();
    assert_eq!(child.status, RunStatus::Failed);
    let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
    let (name, message) = error(&child_events).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(
        message,
        "subagent budget exhausted (30 tokens, $0.0400 spent)"
    );
    let exceeded: Vec<_> = budgets(&child_events)
        .into_iter()
        .filter(|b| b.kind == BudgetKind::Exceeded)
        .collect();
    assert_eq!((exceeded.len(), exceeded[0].max_tokens), (1, Some(30)));
    // The child's own warning fires once, at 90% of its cap, before the
    // refusal; the parent's tree has none.
    let kinds: Vec<BudgetKind> = budgets(&child_events).iter().map(|b| b.kind).collect();
    assert_eq!(kinds, [BudgetKind::Warning, BudgetKind::Exceeded]);

    // The parent's log shows a named tool error, and its run is fine.
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        [
            "Subagent encountered an error: BudgetExceededError: subagent budget \
             exhausted (30 tokens, $0.0400 spent). Partial output: working"
        ]
    );
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert!(
        budgets(&envelopes).is_empty(),
        "the parent's tree is not over"
    );
}

#[tokio::test]
async fn the_parent_budget_still_counts_a_capped_child_s_spend() {
    // The parent spends 15, its child 30: the parent's cap of 45 is reached
    // by the tree before the child's cap of 100.
    let stores = Arc::new(Stores::default());
    let family = tree();
    let agent = built(
        with_budget(Some(45), None),
        family.clone(),
        stores.clone(),
        definition(tokens(100)),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let calls = family.calls.lock().unwrap().clone();
    assert_eq!(calls.get(&("parent".into(), 1)), None, "{calls:?}");
    let (name, message) = error(&envelopes).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(message, "run budget exhausted (45 tokens, $0.0600 spent)");
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
    assert_eq!(paid_tokens(&child_events), 30);
}

#[tokio::test]
async fn an_exhausted_tree_refuses_a_capped_child_before_its_own_cap() {
    let stores = Arc::new(Stores::default());
    let family = tree();
    let agent = built(
        with_budget(Some(15), None),
        family.clone(),
        stores.clone(),
        definition(tokens(100)),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
    let (name, message) = error(&child_events).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert!(message.starts_with("run budget exhausted"), "{message}");
}

#[tokio::test]
async fn a_cost_cap_counts_the_priced_cost_of_the_child() {
    // 0.02 USD a call: a cap of 0.04 refuses the third.
    let stores = Arc::new(Stores::default());
    let family = tree();
    let budget = Some(RunBudget {
        max_cost_usd: Some(0.04),
        ..RunBudget::default()
    });
    let agent = built(spec(), family.clone(), stores.clone(), definition(budget));
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let child_id = RunId::new(format!("{run_id}:sub_0"));
    let child_events = stores.runs.events(NS, &child_id, 0).await.unwrap();
    let (name, message) = error(&child_events).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(
        message,
        "subagent budget exhausted (30 tokens, $0.0400 spent)"
    );
}

#[tokio::test]
async fn a_child_cap_that_cannot_run_is_refused_at_build() {
    let runs = Runs::default();
    let build = |budget: Option<RunBudget>, priced: bool| {
        let builder = AgentBuilder::new(spec())
            .provider(Arc::new(Family::new(Vec::new(), Vec::new())))
            .tools(registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]))
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(Arc::new(Stores::default()))
            .session_store(Arc::new(Stores::default()))
            .namespace(NS)
            .subagents(vec![definition(budget)]);
        match priced {
            true => builder.pricing(price),
            false => builder,
        }
        .build()
        .err()
    };
    let cost = Some(RunBudget {
        max_cost_usd: Some(1.0),
        ..RunBudget::default()
    });
    assert!(
        matches!(build(cost.clone(), false), Some(BuildError::Invalid(m)) if m.contains(CAPPED))
    );
    assert!(build(cost, true).is_none());
    let nan = Some(RunBudget {
        max_cost_usd: Some(f64::NAN),
        ..RunBudget::default()
    });
    assert!(matches!(build(nan, true), Some(BuildError::Invalid(_))));
}

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
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

/// A crash at any write, then a resume: the child never makes a call past
/// its cap, and what it spent before the crash still counts. A crash that
/// left unknown use fails the child with `BudgetAccountingError`, as a run
/// budget does.
#[tokio::test]
async fn a_resumed_child_keeps_what_it_spent_against_its_cap() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = built(spec(), tree(), stores.clone(), definition(tokens(30)));
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    let (mut resumed_children, mut accounting) = (0, 0);
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let family = tree();
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let stream = built(spec(), family.clone(), dying, definition(tokens(30)))
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");

            // Was a child cut off mid-flight, having paid for a call?
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let cut_off = match stores.runs.load(NS, &child_id).await {
                Ok(record) if !record.status.is_terminal() => {
                    let events = stores.runs.events(NS, &child_id, 0).await.unwrap();
                    paid_tokens(&events) > 0
                }
                _ => false,
            };
            let agent = built(
                spec(),
                family.clone(),
                stores.clone(),
                definition(tokens(30)),
            );
            let run_id = recover(&agent, &stores, run_id).await;

            let calls = family.calls.lock().unwrap().clone();
            assert_eq!(
                calls.get(&("check x".to_owned(), 2)),
                None,
                "{context}: a call past the cap: {calls:?}"
            );
            let child_id = RunId::new(format!("{run_id}:sub_0"));
            let Ok(child_events) = stores.runs.events(NS, &child_id, 0).await else {
                continue;
            };
            if child_events.is_empty() {
                continue;
            }
            let (name, message) = error(&child_events).unwrap();
            if unknown_use(&child_events) {
                assert_eq!(name, "BudgetAccountingError", "{context}");
                accounting += 1;
                continue;
            }
            assert_eq!(name, "BudgetExceededError", "{context}");
            assert_eq!(
                message, "subagent budget exhausted (30 tokens, $0.0400 spent)",
                "{context}"
            );
            assert_eq!(paid_tokens(&child_events), 30, "{context}");
            resumed_children += usize::from(cut_off);
        }
    }
    assert!(
        resumed_children > 0,
        "no crash cut a child off with its spend paid"
    );
    assert!(accounting > 0, "no crash left a call of unknown use");
}

#[tokio::test]
async fn a_capped_child_offers_and_allows_what_an_uncapped_one_does() {
    let offered = |budget: Option<RunBudget>| async move {
        let family = tree();
        let agent = built(
            spec(),
            family.clone(),
            Arc::new(Stores::default()),
            definition(budget),
        );
        finish(agent.run("go", RunOptions::new("s1"))).await;
        let first = family.requests_of("check x").remove(0);
        let mut tools: Vec<String> = first.tools.iter().map(|t| t.name.clone()).collect();
        tools.sort();
        tools
    };
    let capped = offered(tokens(30)).await;
    let uncapped = offered(None).await;
    assert_eq!(capped, uncapped);
    assert!(!capped.iter().any(|name| name == "Subagent"), "{capped:?}");
}

/// A host that runs the subagent itself (`Agent::subagent` and
/// `run_child`) gets the cap too.
#[tokio::test]
async fn a_host_run_child_takes_the_cap() {
    let stores = Arc::new(Stores::default());
    let replay = Arc::new(Replay::new(script(3, 1, "Read")));
    let agent = built(spec(), replay, stores.clone(), definition(tokens(30)));
    let child = agent.subagent(Some(CAPPED)).unwrap();
    let stream = child.run_child("go", RunOptions::new("c1"), RunId::new("host"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let (name, message) = error(&events).unwrap();
    assert_eq!(name, "BudgetExceededError");
    assert_eq!(
        message,
        "subagent budget exhausted (30 tokens, $0.0400 spent)"
    );
}

/// The digests taken on the base, before a definition could carry a cap: a
/// definition without one keeps its parent's contract and its child's.
#[test]
fn a_definition_without_a_cap_keeps_the_contracts_it_had() {
    let runs = Runs::default();
    let build = |definition: SubagentDefinition| {
        AgentBuilder::new(spec())
            .provider(Arc::new(Family::new(Vec::new(), Vec::new())))
            .tools(registry(vec![
                Echo::new("Echo", ToolScope::Read, &runs),
                Echo::new("Read", ToolScope::Read, &runs),
            ]))
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(Arc::new(Stores::default()))
            .session_store(Arc::new(Stores::default()))
            .namespace(NS)
            .subagents(vec![definition])
            .build()
            .unwrap()
    };
    let mut plain = SubagentDefinition::new(CAPPED, "a helper", "Help.");
    plain.tools = Some(vec!["Read".into()]);
    let wire = serde_json::to_value(&plain).unwrap();
    assert!(wire.get("budget").is_none(), "an unset cap is off the wire");

    let agent = build(plain.clone());
    assert_eq!(
        agent.contract().to_string(),
        "e287c9b850351b7d353e0fb5ac2f596dd1824151740fc2ed657eec6645d1d801"
    );
    let child = agent.subagent(Some(CAPPED)).unwrap();
    assert_eq!(
        child.contract().to_string(),
        "a997502a048a81fbc22d7befa27c5ed983120ca1266ca571f42f51acf6bf74cd"
    );

    // A cap is part of both contracts: a child never resumes under another.
    let mut capped = plain;
    capped.budget = tokens(30);
    let agent_capped = build(capped);
    assert_ne!(agent_capped.contract(), agent.contract());
    let child_capped = agent_capped.subagent(Some(CAPPED)).unwrap();
    assert_ne!(child_capped.contract(), child.contract());
}

/// A reload is checked like a build: a cost cap on a model with no price
/// is refused, and the agent keeps the roster it had.
#[test]
fn a_reload_with_a_cost_cap_the_pricing_cannot_count_is_refused() {
    let runs = Runs::default();
    let build = |priced: bool| {
        let builder = AgentBuilder::new(spec())
            .provider(Arc::new(Family::new(Vec::new(), Vec::new())))
            .tools(registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]))
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(Arc::new(Stores::default()))
            .session_store(Arc::new(Stores::default()))
            .namespace(NS)
            .subagents(Vec::new());
        match priced {
            true => builder.pricing(price),
            false => builder,
        }
        .build()
        .unwrap()
    };
    let cost = Some(RunBudget {
        max_cost_usd: Some(1.0),
        ..RunBudget::default()
    });
    let unpriced = build(false);
    let refused = unpriced.reload_subagents(vec![definition(cost.clone())]);
    assert!(matches!(refused, Err(BuildError::Invalid(m)) if m.contains(CAPPED)));
    assert!(
        unpriced
            .reload_subagents(vec![definition(tokens(30))])
            .is_ok()
    );
    assert!(build(true).reload_subagents(vec![definition(cost)]).is_ok());
}
