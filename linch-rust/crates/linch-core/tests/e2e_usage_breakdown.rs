//! Usage broken down by the model that served each call and by the
//! subagent that incurred it: the breakdowns sum to the totals the result
//! already reports, across a fallback, a subagent and a crash.

mod common;

use std::collections::BTreeMap;
use std::sync::Arc;

use common::flaky::{Flaky, busy};
use common::subagents::{Dying, Family, spawn_turn};
use common::*;
use linch_core::events::kinds::ResultEvent;
use linch_core::events::model_call::ModelCallReceiptEvent;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, PermissionMode, Provider, ProviderRetry,
    RunId, RunOptions, RunStore, SessionStore, StoreError, ToolScope, Usage,
};

const FALLBACK: &str = "fallback-a";
const CHILD_MODEL: &str = "child-model";

fn sum<'a>(usages: impl IntoIterator<Item = &'a Usage>) -> Usage {
    usages.into_iter().fold(Usage::default(), |a, b| a + *b)
}

fn result_of(events: &[Envelope]) -> ResultEvent {
    events
        .iter()
        .find_map(|e| match &e.event {
            Event::Result(result) => Some(result.clone()),
            _ => None,
        })
        .expect("the run has a result")
}

/// The usage of the receipts in `events`, per serving model (the run's
/// own model when the provider named none).
fn paid_by_model(events: &[Envelope]) -> BTreeMap<String, Usage> {
    let mut paid: BTreeMap<String, Usage> = BTreeMap::new();
    for envelope in events {
        if let Event::ModelCallReceipt(ModelCallReceiptEvent {
            usage: Some(usage),
            actual_model,
            ..
        }) = &envelope.event
        {
            let model = actual_model.clone().unwrap_or_else(|| MODEL.into());
            *paid.entry(model).or_default() += *usage;
        }
    }
    paid
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
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS);
    match subagents {
        true => builder.subagents(Vec::new()),
        false => builder,
    }
    .build()
    .unwrap()
}

/// The parent spawns one child, which runs on another model.
fn tree() -> Arc<Family> {
    let child_call = TurnBuilder::new(CHILD_MODEL)
        .text("working")
        .tool_call("child_call", "Read", &serde_json::json!({"value": "x"}))
        .usage(usage(20, 7))
        .build();
    let child_done = TurnBuilder::new(CHILD_MODEL)
        .text("x holds")
        .usage(usage(30, 9))
        .build();
    let parent = vec![spawn_turn(None, &["check x"]), text_turn("done")];
    Arc::new(Family::new(parent, vec![child_call, child_done]))
}

/// A run that fails over mid-run reports each model's own usage; the
/// per-model sum is the total, and the usage events carry it running.
#[tokio::test]
async fn a_fallback_splits_the_usage_by_serving_model() {
    let mut spec = spec();
    spec.fallback_models = vec![FALLBACK.into()];
    spec.provider_retry = ProviderRetry {
        max_attempts: 1,
        base_delay_ms: 1,
        max_delay_ms: 1_000,
        jitter: 0.0,
    };
    // Turn 0 is served by MODEL, turn 1 fails over to FALLBACK.
    let provider = Arc::new(
        Flaky::new(vec![tool_turn(0, 1, "Read"), text_turn("done")]).fail_with(1, MODEL, busy()),
    );
    let agent = built(spec, provider, Arc::new(Stores::default()), false);
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;

    let result = result_of(&events);
    let want: BTreeMap<String, Usage> = [
        (MODEL.to_owned(), usage(10, 5)),
        (FALLBACK.to_owned(), usage(10, 5)),
    ]
    .into();
    assert_eq!(result.usage_by_model, want);
    assert_eq!(sum(result.usage_by_model.values()), result.total_usage);
    assert_eq!(result.total_usage, usage(20, 10));
    assert!(result.subagent_usage.is_empty());

    let running: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::Usage(u) => Some(u.cumulative_by_model.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(running.len(), 2);
    assert_eq!(running[0], [(MODEL.to_owned(), usage(10, 5))].into());
    assert_eq!(running[1], want);
}

/// A subagent's usage is attributed to it, by the model it ran on, and
/// the parent's own breakdown still sums to its own total.
#[tokio::test]
async fn a_subagent_s_usage_is_attributed_to_it() {
    let stores = Arc::new(Stores::default());
    let agent = built(spec(), tree(), stores.clone(), true);
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let events = finish(stream).await;

    let result = result_of(&events);
    assert_eq!(
        result.usage_by_model,
        [(MODEL.to_owned(), usage(20, 10))].into()
    );
    assert_eq!(sum(result.usage_by_model.values()), result.total_usage);

    let child_id = format!("{run_id}:sub_0");
    let [(key, child)] = result.subagent_usage.iter().collect::<Vec<_>>()[..] else {
        panic!("one subagent: {:?}", result.subagent_usage);
    };
    assert_eq!(key, &child_id);
    assert_eq!(child.subagent_type, None, "the call named no type");
    assert_eq!(child.total_usage, usage(50, 16));
    assert!(child.usage_complete);
    assert_eq!(
        child.usage_by_model,
        [(CHILD_MODEL.to_owned(), usage(50, 16))].into()
    );

    // The whole tree, by model, is what the durable receipts paid.
    let child_events = stores
        .runs
        .events(NS, &RunId::new(child_id), 0)
        .await
        .unwrap();
    let own_events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let mut tree: BTreeMap<String, Usage> = result.usage_by_model.clone();
    for (model, usage) in &child.usage_by_model {
        *tree.entry(model.clone()).or_default() += *usage;
    }
    let mut paid = paid_by_model(&own_events);
    for (model, usage) in paid_by_model(&child_events) {
        *paid.entry(model).or_default() += usage;
    }
    assert_eq!(tree, paid);
}

/// A subagent call that names a type reports it.
#[tokio::test]
async fn a_named_subagent_type_is_reported() {
    let child = vec![text_turn("x holds")];
    let parent = vec![
        spawn_turn(Some("verification"), &["check x"]),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, child));
    let agent = built(spec(), family, Arc::new(Stores::default()), true);
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    let result = result_of(&events);
    let kinds: Vec<_> = (result.subagent_usage.values())
        .map(|s| s.subagent_type.clone())
        .collect();
    assert_eq!(kinds, [Some("verification".to_owned())]);
}

/// A crash at any write, then a resume: the finished run's breakdowns
/// still sum to its totals, and what a resumed run reports for its child
/// is what the child's durable receipts paid.
#[tokio::test]
async fn the_breakdowns_survive_a_crash_and_resume() {
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = built(spec(), tree(), stores.clone(), true);
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    let mut resumed_with_child = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let family = tree();
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let stream =
                built(spec(), family.clone(), dying, true).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = built(spec(), family.clone(), stores.clone(), true);
            match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => {
                    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                }
                Err(StoreError::NotFound { .. }) => continue,
                Err(error) => panic!("{context}: {error}"),
            }
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let result = result_of(&events);
            assert_eq!(
                sum(result.usage_by_model.values()),
                result.total_usage,
                "{context}"
            );
            assert_eq!(
                result.usage_by_model,
                paid_by_model(&events),
                "{context}: own receipts"
            );
            for (child_id, child) in &result.subagent_usage {
                let child_events = stores
                    .runs
                    .events(NS, &RunId::new(child_id.clone()), 0)
                    .await
                    .unwrap();
                assert_eq!(
                    child.usage_by_model,
                    paid_by_model(&child_events),
                    "{context}: child receipts"
                );
                assert_eq!(
                    sum(child.usage_by_model.values()),
                    child.total_usage,
                    "{context}"
                );
                resumed_with_child += 1;
            }
        }
    }
    assert!(resumed_with_child > 0, "no run reported its child");
}

/// A summary call served by another model: the compaction receipt's usage
/// is in the total, so it is in the breakdown under its own model.
struct Summarizing {
    turns: Vec<linch_core::ScriptedTurn>,
}

#[async_trait::async_trait]
impl Provider for Summarizing {
    fn id(&self) -> &str {
        "summarizing"
    }

    async fn stream(
        &self,
        request: linch_core::ProviderRequest,
        _cancel: tokio_util::sync::CancellationToken,
    ) -> Result<linch_core::ProviderStream, linch_core::ProviderError> {
        use futures::StreamExt;
        let turn = if is_summary_call(&request) {
            TurnBuilder::new("summary-model")
                .thinking("nothing to add", None)
                .usage(usage(30, 40))
                .build()
        } else {
            let at = (request.messages.iter())
                .filter(|m| m.role == linch_core::Role::Assistant)
                .count();
            self.turns[at].clone()
        };
        let events: Vec<_> = turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

#[tokio::test]
async fn a_compaction_summary_shows_under_its_own_model() {
    let mut spec = spec();
    spec.compaction = Some(linch_core::CompactionSpec {
        keep_recent_turns: 1,
        ..linch_core::CompactionSpec::new(1)
    });
    let provider = Arc::new(Summarizing {
        turns: script(3, 1, "Read"),
    });
    let agent = built(spec, provider, Arc::new(Stores::default()), false);
    let events = finish(agent.run("go", RunOptions::new("s1"))).await;
    let result = result_of(&events);

    let summaries = result.usage_by_model.get("summary-model").copied();
    assert!(summaries.is_some(), "{:?}", result.usage_by_model);
    assert!(summaries.is_some_and(|u| u.input_tokens > 0 && u.input_tokens % 30 == 0));
    assert!(result.usage_by_model.contains_key(MODEL));
    assert_eq!(sum(result.usage_by_model.values()), result.total_usage);
}
