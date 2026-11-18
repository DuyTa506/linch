//! Provider reliability: once a model has no retry left for a retryable
//! failure, the run fails over to the next of the spec's
//! `fallback_models`, says so in a `model_fallback` event, and keeps that
//! model for the rest of the run, a resumed run included. Usage and cost
//! name the model that answered.

mod common;

use std::sync::Arc;

use common::flaky::{Flaky, busy};
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::{ModelFallbackEvent, ResultSubtype};
use linch_core::provider::scripted::{ScriptedError, TurnBuilder};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, CompactionSpec, Envelope, Event, PermissionMode, ProviderRetry,
    RunOptions, ScriptedTurn, Skill, ToolScope, Usage,
};
use serde_json::json;

const FIRST: &str = "fallback-a";
const SECOND: &str = "fallback-b";

/// A spec that fails over to `FIRST`, then `SECOND`, after `max_attempts`
/// calls to each model (1: no retry).
fn failing_over(max_attempts: u32) -> AgentSpec {
    let mut spec = spec();
    spec.fallback_models = vec![FIRST.into(), SECOND.into()];
    spec.provider_retry = ProviderRetry {
        max_attempts,
        base_delay_ms: 1,
        max_delay_ms: 1_000,
        jitter: 0.0,
    };
    spec
}

/// Prices each output token at 1 USD on `FIRST` and 10 on any other model.
fn priced(spec: AgentSpec, provider: Arc<Flaky>, stores: Arc<Stores>) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .pricing(|model, usage: &Usage| {
            let rate = if model == FIRST { 1.0 } else { 10.0 };
            Some(usage.output_tokens as f64 * rate)
        })
        .build()
        .unwrap()
}

fn fallbacks(envelopes: &[Envelope]) -> Vec<ModelFallbackEvent> {
    (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ModelFallback(fallback) => Some(fallback.clone()),
            _ => None,
        })
        .collect()
}

fn served(envelopes: &[Envelope]) -> Vec<String> {
    (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Usage(usage) => usage.actual_model.clone(),
            _ => None,
        })
        .collect()
}

fn result(envelopes: &[Envelope]) -> &linch_core::events::kinds::ResultEvent {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result,
        other => panic!("the run has no result: {other:?}"),
    }
}

fn calls(models: &[(usize, &str)]) -> Vec<(usize, String)> {
    models.iter().map(|(t, m)| (*t, m.to_string())).collect()
}

/// The model's retries are spent first, then the run fails over: the
/// fallback answers, and its usage and cost name it.
#[tokio::test]
async fn after_its_retries_a_model_fails_over_to_the_next() {
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).down(MODEL));
    let stores = Arc::new(Stores::default());
    let agent = priced(failing_over(2), provider.clone(), stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(
        provider.calls(),
        calls(&[(0, MODEL), (0, MODEL), (0, FIRST)])
    );
    let [fallback] = fallbacks(&envelopes).try_into().unwrap();
    assert_eq!(
        (fallback.from_model.as_str(), fallback.to_model.as_str()),
        (MODEL, FIRST)
    );
    assert_eq!(fallback.error["status"], 503);
    let kinds = kinds(&envelopes);
    let at = |kind: &str| kinds.iter().position(|k| k == kind).unwrap();
    assert!(at("provider_retry") < at("model_fallback"), "{kinds:?}");
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("done"));
    assert_eq!(served(&envelopes), [FIRST]);
    assert_eq!(
        result(&envelopes).total_cost_usd,
        Some(5.0),
        "priced as FIRST"
    );
}

/// A stream that broke after its deltas and has no retry left is voided by
/// the `model_fallback` event, as a retry is by `provider_retry`: it comes
/// after the failed model's deltas and before the fallback's.
#[tokio::test]
async fn a_fallback_event_separates_the_void_deltas_from_the_next_model() {
    let mut broken = TurnBuilder::new(MODEL).text("parti").build();
    broken.events.pop();
    broken.error = Some(ScriptedError::Transport {
        message: "connection reset".into(),
    });
    let provider = Arc::new(Flaky::new(vec![text_turn("whole")]).fail(0, MODEL, broken));
    let agent = priced(failing_over(1), provider, Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let stream: Vec<String> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::PartialAssistant(partial) => Some(format!("text:{}", partial.delta["text"])),
            Event::ModelFallback(_) => Some("model_fallback".into()),
            _ => None,
        })
        .collect();
    assert_eq!(
        stream,
        [r#"text:"parti""#, "model_fallback", r#"text:"whole""#]
    );
}

/// The run keeps the model it failed over to: later turns call it first.
/// Without a retry policy, a failure fails over at once.
#[tokio::test]
async fn the_run_keeps_the_fallback_for_its_later_turns() {
    let provider = Arc::new(
        Flaky::new(vec![tool_turn(0, 1, "Read"), text_turn("done")]).fail_with(0, MODEL, busy()),
    );
    let agent = priced(
        failing_over(1),
        provider.clone(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        provider.calls(),
        calls(&[(0, MODEL), (0, FIRST), (1, FIRST)])
    );
    assert_eq!(fallbacks(&envelopes).len(), 1);
    assert_eq!(served(&envelopes), [FIRST, FIRST]);
}

/// When the last fallback fails too, the run fails with that failure.
#[tokio::test]
async fn when_every_model_fails_the_run_fails_with_the_last_error() {
    let provider = Arc::new(
        Flaky::new(vec![text_turn("done")])
            .down(MODEL)
            .down(FIRST)
            .down(SECOND),
    );
    let agent = priced(
        failing_over(2),
        provider.clone(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        provider.calls().len(),
        6,
        "two calls to each of three models"
    );
    let chain: Vec<_> = (fallbacks(&envelopes).iter())
        .map(|f| (f.from_model.clone(), f.to_model.clone()))
        .collect();
    let want = [(MODEL, FIRST), (FIRST, SECOND)].map(|(a, b)| (a.to_owned(), b.to_owned()));
    assert_eq!(chain, want);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    assert_eq!(
        kinds(&envelopes).iter().filter(|k| *k == "error").count(),
        1
    );
}

/// A failure no other model would fix does not fail over.
#[tokio::test]
async fn a_failure_that_is_not_retryable_does_not_fail_over() {
    let refused = ScriptedError::Status {
        status: 401,
        retry_after_ms: None,
        message: "bad key".into(),
    };
    let provider = Arc::new(Flaky::new(vec![text_turn("done")]).fail_with(0, MODEL, refused));
    let agent = priced(
        failing_over(2),
        provider.clone(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.calls().len(), 1);
    assert!(fallbacks(&envelopes).is_empty());
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
}

/// A run dropped after it failed over, as a crash would drop it, resumes
/// on the fallback: it never calls the model it gave up on again.
#[tokio::test]
async fn a_resumed_run_stays_on_its_fallback() {
    let provider =
        Arc::new(Flaky::new(vec![tool_turn(0, 1, "Read"), text_turn("done")]).down(MODEL));
    let stores = Arc::new(Stores::default());
    let agent = priced(failing_over(1), provider.clone(), stores.clone());
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    while let Some(item) = stream.next().await {
        if matches!(item.unwrap().event, Event::ModelFallback(_)) {
            break;
        }
    }
    drop(stream);
    let resumed = finish(agent.resume(run_id, RunOptions::new("s1"))).await;
    assert_eq!(result(&resumed).final_text.as_deref(), Some("done"));
    let after: Vec<_> = provider
        .calls()
        .into_iter()
        .skip(1)
        .map(|(_, m)| m)
        .collect();
    assert!(after.iter().all(|m| m == FIRST), "{after:?}");
    assert_eq!(served(&resumed).last().map(String::as_str), Some(FIRST));
}

/// A turn that failed over calls the fallback even where a loaded skill
/// names a model; later skill turns get the skill's model again.
#[tokio::test]
async fn a_turn_that_failed_over_calls_the_fallback_over_a_skill_s_model() {
    let mut review = Skill::new("review", "/skills/review", "Review.", "review $ARGUMENTS");
    review.model = Some("other-model".into());
    let skill_call = TurnBuilder::new(MODEL)
        .tool_call("k1", "Skill", &json!({"skill": "review", "args": "a.rs"}))
        .usage(usage(10, 5))
        .build();
    let turns = vec![skill_call, tool_turn(1, 1, "Read"), text_turn("done")];
    let provider = Arc::new(Flaky::new(turns).fail_with(1, "other-model", busy()));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(failing_over(1))
        .provider(provider.clone())
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .skills(vec![review])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = calls(&[(0, MODEL), (1, "other-model"), (1, FIRST), (2, FIRST)]);
    assert_eq!(provider.calls(), want);
}

/// After failing over, a compaction summary call goes to the fallback too.
#[tokio::test]
async fn a_compaction_after_failing_over_asks_the_fallback() {
    let turns: Vec<ScriptedTurn> = vec![
        tool_turn(0, 1, "Read"),
        tool_turn(1, 1, "Read"),
        text_turn("done"),
    ];
    let mut spec = failing_over(1);
    let mut compaction = CompactionSpec::new(1);
    compaction.keep_recent_turns = 1;
    spec.compaction = Some(compaction);
    // The compacted view changes which turn the provider answers.
    spec.max_turns = Some(6);
    let provider = Arc::new(Flaky::new(turns).down(MODEL));
    let agent = priced(spec, provider.clone(), Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert!(
        kinds(&envelopes).contains(&"compaction".to_owned()),
        "{:?}",
        kinds(&envelopes)
    );
    let summary = (provider.requests().into_iter()).find(is_summary_call);
    assert_eq!(summary.expect("a summary call").model, FIRST);
}

/// A subagent's child run takes the parent's spec, so it retries and fails
/// over on its own; the parent's next call stays on the parent's model.
#[tokio::test]
async fn a_subagents_child_retries_and_fails_over_on_its_own() {
    let failing = || ScriptedTurn::failing(busy());
    let turns = vec![
        common::subagents::spawn_turn(Some("scout"), &["look"]),
        failing(),
        failing(),
        text_turn("found it"),
        text_turn("done"),
    ];
    let provider = Arc::new(linch_core::provider::scripted::ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let mut scout = linch_core::SubagentDefinition::new("scout", "Reads around.", "Scout.");
    scout.tools = Some(vec!["Read".into()]);
    let agent = AgentBuilder::new(failing_over(2))
        .provider(provider.clone())
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(vec![scout])
        .build()
        .unwrap();
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(result(&envelopes).subtype, ResultSubtype::Success);
    let models: Vec<String> = provider
        .requests()
        .iter()
        .map(|r| r.model.clone())
        .collect();
    assert_eq!(models, [MODEL, MODEL, MODEL, FIRST, MODEL]);
    let child: Vec<String> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Subagent(sub) => Some(kind(&sub.event)),
            _ => None,
        })
        .filter(|k| k == "provider_retry" || k == "model_fallback")
        .collect();
    assert_eq!(child, ["provider_retry", "model_fallback"]);
    assert!(
        fallbacks(&envelopes).is_empty(),
        "the parent never failed over"
    );
}
