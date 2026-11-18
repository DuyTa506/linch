//! The context budget (`AgentSpec::context_budget`): the context hooks add
//! blocks to a request, and what does not fit is dropped by the declared
//! order, reported on the `context_build` event, and never fails the run.
//! Retrieved memory and file memory are hooks like any other, so both
//! count; the system prompt, the tools and the log are not counted.

mod common;

use std::sync::Arc;

use common::hooks::hook;
use common::*;
use linch_core::hooks::{ContextBuild, ContextBuildResponse};
use linch_core::{
    Agent, AgentBuilder, ContextBudget, Envelope, Event, Hooks, PermissionEngine, PermissionMode,
    PermissionPolicy, RunError, RunOptions, RunStatus, RunStore, ScriptedProvider, TextBlock,
    ToolScope,
};
use serde_json::{Value, json};

const FILES: &str = "linch.deep-agent.file-memory-context";

/// A block of `tokens` estimated tokens, led by `label`.
fn text(label: &str, tokens: usize) -> TextBlock {
    let pad = "x".repeat(tokens * 4 - label.len());
    TextBlock {
        text: format!("{label}{pad}"),
    }
}

fn answer(blocks: Vec<TextBlock>) -> Result<ContextBuildResponse, linch_core::CallbackError> {
    Ok(ContextBuildResponse {
        blocks,
        ..Default::default()
    })
}

/// File memory (one block), retrieved memory (three chunks, best first) and
/// a pinned policy block, as the three kinds of context a host registers.
fn memory_hooks() -> Hooks {
    let hooks = Hooks::default();
    let files = hook::<ContextBuild>(FILES, |_| answer(vec![text("FILES", 10)]));
    let _ = hooks.context_build.register(files.arc()).unwrap();
    let memory = hook::<ContextBuild>("memory", |_| {
        answer((0..3).map(|i| text(&format!("MEM{i}"), 10)).collect())
    });
    let _ = hooks.context_build.register(memory.arc()).unwrap();
    let policy = hook::<ContextBuild>("policy", |_| answer(vec![text("POLICY", 10)]));
    let _ = hooks.context_build.register(policy.arc()).unwrap();
    hooks
}

fn budgeted(
    budget: Option<ContextBudget>,
    hooks: Hooks,
) -> (Agent, Arc<ScriptedProvider>, Arc<Stores>) {
    let provider = Arc::new(ScriptedProvider::new([text_turn("done")]));
    let stores = Arc::new(Stores::default());
    let mut spec = spec();
    spec.context_budget = budget;
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    (agent, provider, stores)
}

fn sent(provider: &ScriptedProvider) -> Vec<String> {
    let requests = provider.requests();
    let tail = requests[0].messages.last().unwrap();
    (tail.content.iter())
        .filter_map(|block| match block {
            linch_core::ContentBlock::Text(t) => {
                Some(t.text.chars().take_while(|c| *c != 'x').collect())
            }
            _ => None,
        })
        .collect()
}

fn builds(envelopes: &[Envelope]) -> Vec<Value> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ContextBuild(build) => Some(Value::Object(build.budget.clone())),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn without_a_budget_every_block_is_sent_and_the_event_reports_none() {
    let (agent, provider, _) = budgeted(None, memory_hooks());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        sent(&provider),
        ["go", "FILES", "MEM0", "MEM1", "MEM2", "POLICY"]
    );
    assert_eq!(builds(&envelopes), [json!({})]);
}

#[tokio::test]
async fn over_budget_drops_the_declared_hooks_first_and_records_it() {
    let mut budget = ContextBudget::new(30);
    budget.drop_order = vec!["memory".into(), FILES.into()];
    budget.pinned = vec!["policy".into()];
    let (agent, provider, stores) = budgeted(Some(budget), memory_hooks());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    // 50 tokens, 30 allowed: the last two chunks of `memory` go, no more.
    assert_eq!(sent(&provider), ["go", "FILES", "MEM0", "POLICY"]);
    assert_eq!(
        builds(&envelopes),
        [json!({
            "max_tokens": 30, "used_tokens": 30, "remaining_tokens": 0, "trimmed": true,
            "dropped": [
                {"source": "memory", "index": 1, "tokens": 10},
                {"source": "memory", "index": 2, "tokens": 10},
            ]
        })]
    );
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    assert_eq!(status, RunStatus::Completed, "the run never fails for it");
}

#[tokio::test]
async fn file_memory_and_retrieval_both_count_and_the_unlisted_go_first() {
    // Nothing is listed: the later hook's blocks go first, so `policy`,
    // then the chunks of `memory` from the last, before `FILES`.
    let (agent, provider, _) = budgeted(Some(ContextBudget::new(20)), memory_hooks());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(sent(&provider), ["go", "FILES", "MEM0"]);
    let report = &builds(&envelopes)[0];
    let gone: Vec<_> = report["dropped"].as_array().unwrap().iter().collect();
    let names: Vec<_> = gone.iter().map(|d| d["source"].as_str().unwrap()).collect();
    assert_eq!(names, ["memory", "memory", "policy"]);
}

#[tokio::test]
async fn pinned_blocks_over_the_budget_stay_and_the_event_says_by_how_much() {
    let mut budget = ContextBudget::new(5);
    budget.pinned = vec!["policy".into()];
    let (agent, provider, _) = budgeted(Some(budget), memory_hooks());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(sent(&provider), ["go", "POLICY"]);
    let report = &builds(&envelopes)[0];
    assert_eq!(report["over_by"], 5);
    assert_eq!(report["used_tokens"], 10);
}

#[tokio::test]
async fn the_budget_is_decided_again_for_every_request() {
    let hooks = Hooks::default();
    let memory = hook::<ContextBuild>("memory", |request| {
        let n = request.messages.len();
        answer((0..n).map(|i| text(&format!("M{n}-{i}"), 10)).collect())
    });
    let _ = hooks.context_build.register(memory.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("done"),
    ]));
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let mut spec = spec();
    spec.context_budget = Some(ContextBudget::new(20));
    let agent = AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let reports = builds(&envelopes);
    assert_eq!(reports.len(), 2);
    assert_eq!(reports[0]["trimmed"], false, "one message, one block");
    assert_eq!(reports[1]["trimmed"], true, "three messages, three blocks");
    assert_eq!(reports[1]["dropped"][0]["index"], 2);
}

#[tokio::test]
async fn the_dropped_text_is_in_neither_the_log_nor_the_journal() {
    let mut budget = ContextBudget::new(10);
    budget.drop_order = vec!["memory".into()];
    let provider = Arc::new(ScriptedProvider::new([text_turn("done")]));
    let stores = Arc::new(Stores::default());
    let mut spec = spec();
    spec.context_budget = Some(budget);
    let agent = AgentBuilder::new(spec)
        .provider(provider)
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(memory_hooks()))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let log = serde_json::to_string(&messages(&*stores, "s1").await).unwrap();
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let events = serde_json::to_string(&events).unwrap();
    for gone in ["MEM1", "MEM2", "FILES", "POLICY"] {
        assert!(!log.contains(gone), "the session holds {gone}");
        assert!(!events.contains(gone), "the journal holds {gone}");
    }
}

/// A run suspended under one budget is not resumed by an agent with
/// another, or none: the budget is part of the contract.
#[tokio::test]
async fn a_run_is_not_resumed_under_another_budget() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let build = |budget: Option<ContextBudget>| -> Agent {
        let tools = registry(vec![Echo::new("save", ToolScope::Write, &runs)]);
        let provider = Arc::new(ScriptedProvider::new(script(1, 1, "save")));
        let policy = PermissionPolicy::new(PermissionMode::Default, "/");
        let engine = PermissionEngine::new(policy).unwrap();
        let mut spec = spec();
        spec.context_budget = budget;
        agent_with(spec, provider, tools, engine, stores.clone())
    };
    let first = build(Some(ContextBudget::new(100)));
    let stream = first.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert!(matches!(
        envelopes.last().unwrap().event,
        Event::Suspended(_)
    ));
    for budget in [Some(ContextBudget::new(101)), None] {
        let changed = build(budget);
        let (_, error) = drain(changed.resume(run_id.clone(), RunOptions::new(""))).await;
        assert!(
            matches!(error, Some(RunError::ContractMismatch { .. })),
            "{error:?}"
        );
    }
}
