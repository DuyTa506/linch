//! Final-answer verifiers: built-in checks that send a failed answer back
//! with feedback while their budget lasts, then accept it (or fail the run
//! at once for a `stop` verifier); a `stop` hook's retry drawing on the same
//! budget; the schema keeping its own; and a crash at every write of a
//! verified run.

mod common;

use std::sync::Arc;

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{ResultEvent, ResultSubtype, VerificationAction};
use linch_core::hooks::{Stop, StopResponse};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::verification::{Check, OnFail, VerificationSpec, Verifier};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Envelope, Event, Hooks, Message, OutputSchema,
    PermissionMode, Provider, RunOptions, RunStatus, RunStore, ScriptedProvider, ScriptedTurn,
    SessionStore,
};
use serde_json::json;

const CITE: &str = "Cite a source as [n].";

fn cites(on_fail: OnFail) -> Verifier {
    Verifier {
        name: "cites".into(),
        check: Check::Matches {
            pattern: r"\[\d+\]".into(),
        },
        feedback: Some(CITE.into()),
        on_fail,
    }
}

fn verifying(max_retries: u32, verifiers: Vec<Verifier>) -> AgentSpec {
    let mut spec = spec();
    spec.verification = Some(VerificationSpec {
        max_retries,
        verifiers,
    });
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
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn result(envelopes: &[Envelope]) -> ResultEvent {
    (envelopes.iter().rev())
        .find_map(|envelope| match &envelope.event {
            Event::Result(result) => Some(result.clone()),
            _ => None,
        })
        .expect("a result")
}

fn verifications(envelopes: &[Envelope]) -> Vec<(String, VerificationAction, u64)> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Verification(event) => {
                Some((event.verifier.clone(), event.action, event.attempt))
            }
            _ => None,
        })
        .collect()
}

fn reminder(text: &str) -> Message {
    Message::user_text(format!("<system-reminder>\n{text}\n</system-reminder>"))
}

fn texts(turns: &[&str]) -> Vec<ScriptedTurn> {
    turns.iter().map(|text| text_turn(text)).collect()
}

/// A failed answer goes back with the verifier's feedback; the next one
/// passes and completes the run.
#[tokio::test]
async fn a_failed_answer_goes_back_with_feedback_and_the_next_one_passes() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(texts(&["no source", "see [1]"])));
    let spec = verifying(2, vec![cites(OnFail::Retry)]);
    let agent = build(spec, provider, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let want = [
        Message::user_text("go"),
        Message::assistant_text("no source"),
        reminder(CITE),
        Message::assistant_text("see [1]"),
    ];
    assert_eq!(messages(&*stores, "s1").await, want);
    assert_eq!(
        verifications(&envelopes),
        [("cites".to_owned(), VerificationAction::Retry, 1)]
    );
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.final_text.as_deref(), Some("see [1]"));
}

/// Past the budget the answer is accepted as it is, with an `exhausted`
/// event, as Python's `FinalAnswerVerifierHook` does.
#[tokio::test]
async fn past_the_budget_the_answer_is_accepted_and_marked_exhausted() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(texts(&["a", "b", "c"])));
    let spec = verifying(1, vec![cites(OnFail::Retry)]);
    let agent = build(spec, provider.clone(), Hooks::default(), stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert_eq!(provider.requests().len(), 2);
    assert_eq!(
        verifications(&envelopes),
        [
            ("cites".to_owned(), VerificationAction::Retry, 1),
            ("cites".to_owned(), VerificationAction::Exhausted, 1),
        ]
    );
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.final_text.as_deref(), Some("b"));
}

/// A `stop` verifier fails the run at once, without a retry.
#[tokio::test]
async fn a_stop_verifier_fails_the_run_at_once() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(texts(&["no source", "[1]"])));
    let spec = verifying(2, vec![cites(OnFail::Stop)]);
    let agent = build(spec, provider.clone(), Hooks::default(), stores);
    let stream = agent.run("go", RunOptions::new("s1"));
    let envelopes = finish(stream).await;

    assert_eq!(provider.requests().len(), 1);
    assert_eq!(
        verifications(&envelopes),
        [("cites".to_owned(), VerificationAction::Stop, 0)]
    );
    let error = (envelopes.iter())
        .find_map(|e| match &e.event {
            Event::Error(error) => Some(error.error.clone()),
            _ => None,
        })
        .expect("an error event");
    assert_eq!(error["name"], "VerificationError");
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert_eq!(result.final_text.as_deref(), Some("no source"));
}

/// Verifiers run in order and the first failure decides; keywords and a
/// forbidden pattern check the text as the model wrote it.
#[tokio::test]
async fn the_first_failing_verifier_decides() {
    let topic = Verifier {
        name: "topic".into(),
        check: Check::Keywords {
            keywords: vec!["refund".into(), "policy".into()],
            any: false,
            case_sensitive: false,
        },
        feedback: None,
        on_fail: OnFail::Retry,
    };
    let quiet = Verifier {
        name: "quiet".into(),
        check: Check::Forbids {
            pattern: "!".into(),
        },
        feedback: Some("No exclamation marks.".into()),
        on_fail: OnFail::Retry,
    };
    let stores = Arc::new(Stores::default());
    let turns = texts(&["Refund!", "Refund Policy!", "refund policy."]);
    let provider = Arc::new(ScriptedProvider::new(turns));
    let spec = verifying(2, vec![topic, quiet]);
    let agent = build(spec, provider, Hooks::default(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        verifications(&envelopes),
        [
            ("topic".to_owned(), VerificationAction::Retry, 1),
            ("quiet".to_owned(), VerificationAction::Retry, 2),
        ]
    );
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        log[2],
        reminder(
            "Your previous answer failed verification: it must mention each of: refund, \
             policy. Revise it and answer again."
        )
    );
    assert_eq!(log[4], reminder("No exclamation marks."));
}

/// With a verification spec, a `stop` hook's retry draws on its budget,
/// durably and reported under the hook's policy id: the client form of a
/// verifier. Without one it is honoured once per run call, as before.
#[tokio::test]
async fn a_stop_hook_retry_draws_on_the_verification_budget() {
    let retrying = || {
        let hooks = Hooks::default();
        let judge = hook::<Stop>("judge", |request| {
            Ok(match request.text.contains('[') {
                true => StopResponse::Allow,
                false => StopResponse::Retry {
                    feedback: CITE.into(),
                },
            })
        });
        let _ = hooks.stop.register(judge.arc()).unwrap();
        hooks
    };
    let answers = || texts(&["a", "b", "c", "d"]);

    let provider = Arc::new(ScriptedProvider::new(answers()));
    let agent = build(
        verifying(2, vec![]),
        provider.clone(),
        retrying(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.requests().len(), 3, "two retries, then accepted");
    assert_eq!(
        verifications(&envelopes),
        [
            ("judge".to_owned(), VerificationAction::Retry, 1),
            ("judge".to_owned(), VerificationAction::Retry, 2),
            ("judge".to_owned(), VerificationAction::Exhausted, 2),
        ]
    );
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("c"));

    let provider = Arc::new(ScriptedProvider::new(answers()));
    let agent = build(
        spec(),
        provider.clone(),
        retrying(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(provider.requests().len(), 2, "one retry per run call");
    assert!(verifications(&envelopes).is_empty());
}

/// A built-in verifier and a `stop` hook share one budget.
#[tokio::test]
async fn built_in_verifiers_and_stop_hooks_share_one_budget() {
    let hooks = Hooks::default();
    let judge = hook::<Stop>("judge", |request| {
        Ok(match request.text.contains("ok") {
            true => StopResponse::Allow,
            false => StopResponse::Retry {
                feedback: "say ok".into(),
            },
        })
    });
    let _ = hooks.stop.register(judge.arc()).unwrap();
    let provider = Arc::new(ScriptedProvider::new(texts(&["x", "[1]", "[2]"])));
    let spec = verifying(2, vec![cites(OnFail::Retry)]);
    let agent = build(spec, provider.clone(), hooks, Arc::new(Stores::default()));
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        verifications(&envelopes),
        [
            ("cites".to_owned(), VerificationAction::Retry, 1),
            ("judge".to_owned(), VerificationAction::Retry, 2),
            ("judge".to_owned(), VerificationAction::Exhausted, 2),
        ]
    );
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("[2]"));
}

/// The schema keeps its own budget: an answer that breaks the schema is
/// retried on `structured_output_retries`, one that fails a verifier on
/// `verification.max_retries`, and each counts its own attempts.
#[tokio::test]
async fn the_schema_and_the_verifiers_spend_separate_budgets() {
    let mut spec = verifying(
        1,
        vec![Verifier {
            name: "positive".into(),
            check: Check::Forbids {
                pattern: "-".into(),
            },
            feedback: None,
            on_fail: OnFail::Retry,
        }],
    );
    spec.output_schema = Some(OutputSchema {
        name: "total".into(),
        schema: object(json!({
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
        })),
        strict: true,
        description: None,
    });
    spec.structured_output_retries = 1;
    let turns = texts(&["nope", "{\"total\": -1}", "{\"total\": 1}"]);
    let provider = Arc::new(ScriptedProvider::new(turns));
    let agent = build(
        spec,
        provider,
        Hooks::default(),
        Arc::new(Stores::default()),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(
        verifications(&envelopes),
        [
            ("output_schema".to_owned(), VerificationAction::Retry, 1),
            ("positive".to_owned(), VerificationAction::Retry, 1),
        ]
    );
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.structured_output, Some(object(json!({"total": 1}))));
}

/// A verifier that would clash with the schema's events is refused when the
/// agent is built.
#[test]
fn a_verifier_that_cannot_run_is_refused_at_build() {
    let mut bad = cites(OnFail::Retry);
    bad.name = "output_schema".into();
    let built = AgentBuilder::new(verifying(1, vec![bad]))
        .provider(Arc::new(ScriptedProvider::new(vec![])))
        .permissions(policy(PermissionMode::Default))
        .run_store(Arc::new(Stores::default()))
        .session_store(Arc::new(Stores::default()))
        .namespace(NS)
        .build();
    assert!(
        matches!(built, Err(BuildError::Invalid(ref why)) if why.contains("reserved")),
        "{:?}",
        built.err()
    );
}

/// The turns of one crash scenario, made afresh for each run.
type Script = fn() -> Vec<ScriptedTurn>;

/// A crash at any write of a verified run replays the retry exactly once:
/// the feedback is logged once, one `verification` retry is durable, and
/// the budget holds across the resume (a counter that restarted would allow
/// a second retry on the second script instead of `exhausted`).
#[tokio::test]
async fn a_crash_at_any_write_of_a_verified_run_replays_the_retry_once() {
    let scripts: [(&str, Script); 2] = [
        ("bad, good", || texts(&["a", "[1]"])),
        ("bad, bad", || texts(&["a", "b", "[1]"])),
    ];
    let spec = || verifying(1, vec![cites(OnFail::Retry)]);
    for (name, turns) in scripts {
        let clean = Arc::new(Stores::default());
        let agent = build(
            spec(),
            Arc::new(Replay::new(turns())),
            Hooks::default(),
            clean.clone(),
        );
        let stream = agent.run("go", RunOptions::new("s1"));
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
                let stream = build(spec(), provider.clone(), Hooks::default(), faulty)
                    .run("go", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                assert!(drain(stream).await.1.is_some(), "{context}");
                let agent = build(spec(), provider.clone(), Hooks::default(), stores.clone());
                let run_id = match stores.runs.load(NS, &run_id).await {
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
                };
                let record = stores.runs.load(NS, &run_id).await.unwrap();
                assert_eq!(record.status, RunStatus::Completed, "{context}");
                assert_eq!(messages(&*stores, "s1").await, want, "{context}");
                let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
                assert_eq!(verifications(&events), want_verifications, "{context}");
                let calls = provider.calls();
                assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
                assert!(!calls.contains_key(&2), "{context}: a third answer");
            }
        }
    }
}
