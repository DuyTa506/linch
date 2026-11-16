//! Truncation recovery: a text answer the output limit cut off goes back
//! to the model with feedback, bounded, and the final answer is every part
//! in order. A crash at any write keeps the parts and the bound.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::kinds::{ResultEvent, UserEventSubtype};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::truncation::DEFAULT_FEEDBACK;
use linch_core::{
    Agent, AgentSpec, Envelope, Event, OutputSchema, PermissionMode, Role, RunId, RunOptions,
    RunStatus, RunStore, ScriptedTurn, SessionStore, StopReason, ToolScope, TruncationRecovery,
};
use serde_json::json;

fn part(text: &str, stop_reason: StopReason) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text(text)
        .usage(usage(10, 5))
        .stop_reason(stop_reason)
        .build()
}

/// An answer the output limit cuts off twice.
fn cut_twice() -> Vec<ScriptedTurn> {
    vec![
        part("Hel", StopReason::MaxTokens),
        part("lo, ", StopReason::MaxTokens),
        part("world", StopReason::EndTurn),
    ]
}

fn recovering(max_attempts: u32) -> AgentSpec {
    let mut spec = spec();
    spec.truncation_recovery = Some(TruncationRecovery {
        max_attempts,
        feedback: None,
    });
    spec
}

fn agent_on<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    stores: Arc<S>,
) -> Agent {
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    agent_with(
        spec,
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        stores,
    )
}

fn result(envelopes: &[Envelope]) -> ResultEvent {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result.clone(),
        other => panic!("no result: {other:?}"),
    }
}

async fn run(spec: AgentSpec, turns: Vec<ScriptedTurn>) -> (Arc<Stores>, Vec<Envelope>) {
    let stores = Arc::new(Stores::default());
    let agent = agent_on(spec, Arc::new(Replay::new(turns)), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    (stores, envelopes)
}

fn reminder() -> String {
    format!("<system-reminder>\n{DEFAULT_FEEDBACK}\n</system-reminder>")
}

#[tokio::test]
async fn a_cut_answer_is_continued_and_stitched() {
    let (stores, envelopes) = run(recovering(2), cut_twice()).await;
    let result = result(&envelopes);
    assert_eq!(result.final_text.as_deref(), Some("Hello, world"));
    assert_eq!(result.stop_reason, StopReason::EndTurn);
    let log = messages(&*stores, "s1").await;
    let shape: Vec<_> = log.iter().map(|m| (m.role, m.text())).collect();
    assert_eq!(
        shape,
        [
            (Role::User, "go".to_owned()),
            (Role::Assistant, "Hel".to_owned()),
            (Role::User, reminder()),
            (Role::Assistant, "lo, ".to_owned()),
            (Role::User, reminder()),
            (Role::Assistant, "world".to_owned()),
        ],
        "each part is logged as the message it was"
    );
    let notes = (envelopes.iter())
        .filter(
            |e| matches!(&e.event, Event::User(u) if u.subtype == UserEventSubtype::Notification),
        )
        .count();
    assert_eq!(notes, 2);
}

/// Each part is scrubbed as a response; a secret the cut split across two
/// parts is whole only in the stitched answer, so that is scrubbed too.
#[tokio::test]
async fn a_secret_split_by_the_cut_is_scrubbed_from_the_answer() {
    use linch_core::redaction::{RedactionRule, RedactionSpec};

    let mut spec = recovering(1);
    spec.redaction = Some(RedactionSpec {
        rules: vec![RedactionRule::new("hunter2")],
        response: true,
        tool_results: true,
        prompt: false,
    });
    let turns = vec![
        part("the password is hun", StopReason::MaxTokens),
        part("ter2, keep it safe", StopReason::EndTurn),
    ];
    let (_, envelopes) = run(spec, turns).await;
    assert_eq!(
        result(&envelopes).final_text.as_deref(),
        Some("the password is [REDACTED], keep it safe")
    );
}

#[tokio::test]
async fn past_the_bound_a_cut_answer_is_final() {
    let (_, envelopes) = run(recovering(1), cut_twice()).await;
    let result = result(&envelopes);
    assert_eq!(result.final_text.as_deref(), Some("Hello, "));
    assert_eq!(result.stop_reason, StopReason::MaxTokens);
}

#[tokio::test]
async fn without_recovery_a_cut_answer_is_final() {
    let (stores, envelopes) = run(spec(), cut_twice()).await;
    assert_eq!(result(&envelopes).final_text.as_deref(), Some("Hel"));
    assert_eq!(messages(&*stores, "s1").await.len(), 2);
}

/// The schema check reads the whole answer, not a cut part: a JSON object
/// split by the output limit is valid once stitched.
#[tokio::test]
async fn the_schema_checks_the_stitched_answer() {
    let mut spec = recovering(1);
    spec.output_schema = Some(OutputSchema {
        name: "answer".into(),
        schema: json!({"type": "object", "properties": {"a": {"type": "integer"}},
                       "required": ["a"]})
        .as_object()
        .unwrap()
        .clone(),
        strict: true,
        description: None,
    });
    let turns = vec![
        part(r#"{"a":"#, StopReason::MaxTokens),
        part(" 1}", StopReason::EndTurn),
    ];
    let (_, envelopes) = run(spec, turns).await;
    let result = result(&envelopes);
    assert_eq!(result.final_text.as_deref(), Some(r#"{"a": 1}"#));
    assert_eq!(
        result.structured_output,
        json!({"a": 1}).as_object().cloned()
    );
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

/// A crash at every write of a twice-continued answer ends with the same
/// stitched answer and log, and never continues more than the bound.
#[tokio::test]
async fn a_crash_at_any_write_keeps_the_parts_and_the_bound() {
    for max_attempts in [1, 2] {
        let (clean, envelopes) = run(recovering(max_attempts), cut_twice()).await;
        let want = result(&envelopes).final_text;
        let want_log = messages(&*clean, "s1").await;
        let writes = clean.log.lock().unwrap().len() as u64;
        for n in 1..=writes {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                let context = format!("{max_attempts}: write {n} failing {timing:?}");
                let stores = Arc::new(Stores::default());
                let provider = Arc::new(Replay::new(cut_twice()));
                let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
                let stream = agent_on(recovering(max_attempts), provider.clone(), faulty)
                    .run("go", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                assert!(drain(stream).await.1.is_some(), "{context}");
                let agent = agent_on(recovering(max_attempts), provider, stores.clone());
                let run_id = recover(&agent, &stores, run_id).await;
                let record = stores.runs.load(NS, &run_id).await.unwrap();
                assert_eq!(record.status, RunStatus::Completed, "{context}");
                let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
                let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
                    panic!("{context}: no result");
                };
                assert_eq!(result.final_text, want, "{context}");
                assert_eq!(messages(&*stores, "s1").await, want_log, "{context}");
            }
        }
    }
}
