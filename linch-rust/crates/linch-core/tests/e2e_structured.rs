//! Structured output: the final answer checked against the agent's output
//! schema, sent back with the error while retries last, the parsed value
//! on the result, the `stop` hook's view of it, and a crash at every write
//! of a retried answer.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{ResultEvent, ResultSubtype, VerificationAction};
use linch_core::hooks::{Stop, StopResponse};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Envelope, Event, Hooks, JsonObject, OutputSchema,
    PermissionMode, Provider, RunOptions, RunStatus, RunStore, ScriptedProvider, ScriptedTurn,
    SessionStore, ToolScope,
};
use serde_json::json;

fn schema() -> OutputSchema {
    OutputSchema {
        name: "invoice".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
            "additionalProperties": false,
        })),
        strict: true,
        description: None,
    }
}

fn shaped(retries: u32) -> AgentSpec {
    let mut spec = spec();
    spec.output_schema = Some(schema());
    spec.structured_output_retries = retries;
    spec
}

fn build<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    hooks: Hooks,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn result(envelopes: &[Envelope]) -> ResultEvent {
    match &envelopes.last().unwrap().event {
        Event::Result(result) => result.clone(),
        other => panic!("ended with {other:?}"),
    }
}

fn verifications(envelopes: &[Envelope]) -> Vec<(VerificationAction, u64)> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Verification(event) => {
                assert_eq!(event.verifier, "output_schema");
                Some((event.action, event.attempt))
            }
            _ => None,
        })
        .collect()
}

fn error_message(envelopes: &[Envelope]) -> String {
    (envelopes.iter())
        .find_map(|envelope| match &envelope.event {
            Event::Error(error) => {
                assert_eq!(error.error["name"], "StructuredOutputError");
                Some(error.error["message"].as_str().unwrap().to_owned())
            }
            _ => None,
        })
        .expect("an error event")
}

fn total(value: u64) -> JsonObject {
    object(json!({"total": value}))
}

#[tokio::test]
async fn a_valid_answer_is_the_results_structured_output() {
    let provider = Arc::new(ScriptedProvider::new([
        tool_turn(0, 1, "Read"),
        text_turn("```json\n{\"total\": 4.5}\n```"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = build(shaped(0), provider.clone(), Hooks::default(), stores);
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(
        result.structured_output,
        Some(object(json!({"total": 4.5})))
    );
    assert_eq!(result.structured_error, None);
    assert!(verifications(&envelopes).is_empty());
    for request in provider.requests() {
        assert_eq!(request.output_schema, Some(schema()), "every primary call");
        assert_eq!(request.tool_choice, None);
    }
}

#[tokio::test]
async fn a_failed_answer_goes_back_with_the_error_and_the_next_one_counts() {
    let provider = Arc::new(ScriptedProvider::new([
        text_turn("the total is 4"),
        text_turn("{\"total\": 4}"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = build(
        shaped(1),
        provider.clone(),
        Hooks::default(),
        stores.clone(),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.structured_output, Some(total(4)));
    assert_eq!(verifications(&envelopes), [(VerificationAction::Retry, 1)]);
    // The feedback is logged, so the model saw exactly what the log holds.
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 4, "{log:?}");
    let feedback = log[2].text();
    assert!(feedback.starts_with("<system-reminder>\n"), "{feedback}");
    assert!(
        feedback.contains("failed structured-output validation: JSON parse error"),
        "{feedback}"
    );
    assert!(feedback.contains("Respond again with ONLY a JSON object"));
    assert_eq!(provider.requests()[1].messages[2].text(), feedback);
}

#[tokio::test]
async fn past_the_bound_the_run_fails_and_says_why() {
    let provider = Arc::new(ScriptedProvider::new([
        text_turn("[1]"),
        text_turn("{\"sum\": 4}"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = build(shaped(1), provider, Hooks::default(), stores.clone());
    let stream = agent.run("bill me", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert_eq!(result.final_text.as_deref(), Some("{\"sum\": 4}"));
    assert_eq!(result.structured_output, None);
    let error = result.structured_error.unwrap();
    assert!(error.starts_with("Schema validation error:"), "{error}");
    assert_eq!(
        verifications(&envelopes),
        [
            (VerificationAction::Retry, 1),
            (VerificationAction::Stop, 1)
        ]
    );
    let message = error_message(&envelopes);
    assert!(message.contains("after 1 retries"), "{message}");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

#[tokio::test]
async fn with_no_retries_the_first_bad_answer_fails_the_run() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("no json here")]));
    let agent = build(
        shaped(0),
        provider,
        Hooks::default(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    assert_eq!(verifications(&envelopes), [(VerificationAction::Stop, 0)]);
    let message = error_message(&envelopes);
    assert!(
        message.starts_with("the final answer does not satisfy the output schema: JSON parse"),
        "{message}"
    );
}

#[tokio::test]
async fn the_retries_count_toward_max_turns() {
    let provider = Arc::new(ScriptedProvider::new([
        text_turn("nope"),
        text_turn("still nope"),
    ]));
    let mut spec = shaped(5);
    spec.max_turns = Some(2);
    let agent = build(
        spec,
        provider,
        Hooks::default(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    assert!(kinds(&envelopes).contains(&"loop_guard".to_owned()));
}

#[tokio::test]
async fn a_bad_schema_or_retries_without_one_does_not_build() {
    let provider: Arc<dyn Provider> = Arc::new(ScriptedProvider::new([]));
    let stores = Arc::new(Stores::default());
    let attempt = |spec: AgentSpec| {
        AgentBuilder::new(spec)
            .provider(provider.clone())
            .permissions(policy(PermissionMode::SkipDangerous))
            .run_store(stores.clone())
            .session_store(stores.clone())
            .namespace(NS)
            .build()
    };
    let mut array = shaped(0);
    array.output_schema.as_mut().unwrap().schema = object(json!({"type": "array"}));
    assert!(matches!(attempt(array), Err(BuildError::Invalid(_))));
    let mut retrying = spec();
    retrying.structured_output_retries = 1;
    assert!(matches!(attempt(retrying), Err(BuildError::Invalid(_))));
}

/// A `stop` hook that records the value each request carried and
/// answers with `answer`.
fn stop_hooks(
    seen: &Arc<Mutex<Vec<Option<JsonObject>>>>,
    answer: impl Fn(&str) -> StopResponse + Send + Sync + 'static,
) -> Hooks {
    let hooks = Hooks::default();
    let seen = Arc::clone(seen);
    let judge = hook::<Stop>("judge", move |request| {
        seen.lock().unwrap().push(request.structured_output.clone());
        Ok(answer(&request.text))
    });
    let _ = hooks.stop.register(judge.arc()).unwrap();
    hooks
}

#[tokio::test]
async fn a_stop_hook_sees_the_parsed_value_and_only_valid_answers() {
    let provider = Arc::new(ScriptedProvider::new([
        text_turn("nope"),
        text_turn("{\"total\": 2}"),
    ]));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = stop_hooks(&seen, |_| StopResponse::Allow);
    let agent = build(shaped(1), provider, hooks, Arc::new(Stores::default()));
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).structured_output, Some(total(2)));
    // The invalid answer went back to the model without asking the hook.
    assert_eq!(*seen.lock().unwrap(), [Some(total(2))]);
}

#[tokio::test]
async fn a_rewritten_answer_is_checked_again() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("{\"total\": 2}")]));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = stop_hooks(&seen, |_| StopResponse::Rewrite {
        text: "{\"total\": 3}".into(),
    });
    let agent = build(shaped(0), provider, hooks, Arc::new(Stores::default()));
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.final_text.as_deref(), Some("{\"total\": 3}"));
    assert_eq!(result.structured_output, Some(total(3)));
}

#[tokio::test]
async fn a_rewrite_that_breaks_the_schema_fails_the_run_without_a_retry() {
    let provider = Arc::new(ScriptedProvider::new([
        text_turn("{\"total\": 2}"),
        text_turn("{\"total\": 9}"),
    ]));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = stop_hooks(&seen, |_| StopResponse::Rewrite {
        text: "about two".into(),
    });
    let agent = build(
        shaped(3),
        provider.clone(),
        hooks,
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert_eq!(result.final_text.as_deref(), Some("about two"));
    assert!(
        result
            .structured_error
            .unwrap()
            .starts_with("JSON parse error")
    );
    assert_eq!(provider.requests().len(), 1, "the model is not asked again");
    assert!(error_message(&envelopes).starts_with("a stop hook rewrote the final answer"));
    assert_eq!(verifications(&envelopes), [(VerificationAction::Stop, 0)]);
}

#[tokio::test]
async fn a_stop_retry_or_continue_has_the_next_answer_checked_on_its_own_budget() {
    for answer in ["retry", "continue"] {
        let provider = Arc::new(ScriptedProvider::new([
            text_turn("{\"total\": 1}"),
            text_turn("one"),
            text_turn("{\"total\": 2}"),
        ]));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let calls = Arc::new(Mutex::new(0));
        let counter = Arc::clone(&calls);
        let hooks = stop_hooks(&seen, move |_| {
            let mut calls = counter.lock().unwrap();
            *calls += 1;
            match (*calls, answer) {
                (1, "retry") => StopResponse::Retry {
                    feedback: "double check".into(),
                },
                (1, _) => StopResponse::Continue {
                    feedback: "keep going".into(),
                },
                _ => StopResponse::Allow,
            }
        });
        let agent = build(shaped(1), provider, hooks, Arc::new(Stores::default()));
        let envelopes = finish(agent.run("bill me", RunOptions::new("s1"))).await;
        let result = result(&envelopes);
        assert_eq!(result.structured_output, Some(total(2)), "{answer}");
        assert_eq!(verifications(&envelopes), [(VerificationAction::Retry, 1)]);
        assert_eq!(*seen.lock().unwrap(), [Some(total(1)), Some(total(2))]);
    }
}

#[tokio::test]
async fn a_subagent_does_not_take_its_parents_schema() {
    let provider = Arc::new(ScriptedProvider::new([]));
    let stores = Arc::new(Stores::default());
    let parent = AgentBuilder::new(shaped(1))
        .provider(provider)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap();
    let child = parent.subagent(None).unwrap();
    assert_eq!(child.spec().output_schema, None);
    assert_eq!(child.spec().structured_output_retries, 0);
}

fn accepted(envelopes: &[Envelope]) -> usize {
    use linch_core::events::model_call::CallDisposition;
    (envelopes.iter())
        .filter(|e| {
            matches!(&e.event, Event::ModelCallReceipt(receipt)
                if receipt.disposition == CallDisposition::Accepted)
        })
        .count()
}

/// The turns of one crash scenario, made afresh for each run.
type Script = fn() -> Vec<ScriptedTurn>;

/// A crash at any write of a run whose answer was retried replays the
/// retry exactly once: the feedback is logged once, one `verification`
/// retry is durable, no paid response is bought again, and the bound
/// holds across the resume (a counter that restarted would allow a second
/// retry on the second script).
#[tokio::test]
async fn a_crash_at_any_write_of_a_retried_answer_replays_the_retry_once() {
    let scripts: [(&str, Script, RunStatus); 2] = [
        (
            "bad, good",
            || vec![text_turn("nope"), text_turn("{\"total\": 1}")],
            RunStatus::Completed,
        ),
        (
            "bad, bad",
            || {
                vec![
                    text_turn("nope"),
                    text_turn("still nope"),
                    text_turn("{\"total\": 1}"),
                ]
            },
            RunStatus::Failed,
        ),
    ];
    for (name, turns, status) in scripts {
        let clean = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(turns()));
        let agent = build(shaped(1), provider, Hooks::default(), clean.clone());
        let stream = agent.run("bill me", RunOptions::new("s1"));
        let clean_id = stream.run_id().clone();
        finish(stream).await;
        let want = messages(&*clean, "s1").await;
        let clean_events = clean.runs.events(NS, &clean_id, 0).await.unwrap();
        let want_verifications = verifications(&clean_events);
        let writes = clean.log.lock().unwrap().len() as u64;
        for n in 1..=writes {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                let context = format!("{name}: write {n} failing {timing:?}");
                let stores = Arc::new(Stores::default());
                let provider = Arc::new(Replay::new(turns()));
                let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
                let stream = build(shaped(1), provider.clone(), Hooks::default(), faulty)
                    .run("bill me", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                assert!(drain(stream).await.1.is_some(), "{context}");
                let agent = build(
                    shaped(1),
                    provider.clone(),
                    Hooks::default(),
                    stores.clone(),
                );
                let run_id = match stores.runs.load(NS, &run_id).await {
                    Ok(record) if record.status.is_terminal() => run_id,
                    Ok(_) => {
                        finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                        run_id
                    }
                    Err(_) => {
                        let stream = agent.run("bill me", RunOptions::new("s1"));
                        let run_id = stream.run_id().clone();
                        finish(stream).await;
                        run_id
                    }
                };
                let record = stores.runs.load(NS, &run_id).await.unwrap();
                assert_eq!(record.status, status, "{context}");
                assert_eq!(messages(&*stores, "s1").await, want, "{context}");
                let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
                assert_eq!(verifications(&events), want_verifications, "{context}");
                assert_eq!(accepted(&events), 2, "{context}: one per answer");
                let calls = provider.calls();
                assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
                assert!(
                    !calls.contains_key(&2),
                    "{context}: a third answer was asked"
                );
            }
        }
    }
}
