//! `AskUser`: a call suspends the run for the user's answer, `answer`
//! commits it as the call's result and continues the run, and a crash at
//! any write replays the answer exactly once.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::subagents::*;
use common::*;
use linch_core::ask_user::{AnswerValue, AskUser, AskUserAnswer};
use linch_core::events::kinds::{InputRequestKind, SuspendStatus};
use linch_core::hooks::ApprovalResponse;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, Message, PermissionMode, Phase, RunError, RunId,
    RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, Tool, ToolRegistry, ToolScope,
};
use serde_json::{Value, json};

/// The input of an `AskUser` call: one single-select question `db`.
fn question() -> Value {
    json!({"questions": [{
        "id": "db", "header": "Database", "question": "Which database?",
        "options": [{"label": "Postgres", "description": "durable"},
                    {"label": "Memory", "description": "for tests"}]
    }]})
}

/// A response that calls `AskUser` as `ask_{turn}`, after `before` calls
/// of `tool` (ids `call_{turn}_{i}`).
fn ask_turn(turn: usize, before: usize, tool: &str) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL).text("asking");
    for i in 0..before {
        let value = json!({"value": format!("v{turn}.{i}")});
        builder = builder.tool_call(&format!("call_{turn}_{i}"), tool, &value);
    }
    let builder = builder.tool_call(&format!("ask_{turn}"), "AskUser", &question());
    builder.usage(usage(10, 5)).build()
}

fn tools(runs: &Runs) -> Arc<ToolRegistry> {
    let registry = ToolRegistry::new();
    let echo: Arc<dyn Tool> = Arc::new(Echo::new("Write", ToolScope::Write, runs));
    for tool in [echo, Arc::new(AskUser::new())] {
        let _ = registry.register(tool).unwrap();
    }
    Arc::new(registry)
}

fn build<S: RunStore + SessionStore + 'static>(
    provider: Arc<Replay>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    agent(
        provider,
        tools(runs),
        policy(PermissionMode::Default),
        stores,
    )
}

fn postgres() -> HashMap<String, AskUserAnswer> {
    let answer = AskUserAnswer::new([("db".to_owned(), AnswerValue::One("Postgres".into()))]);
    HashMap::from([("ask_0".to_owned(), answer)])
}

const ANSWERED: &str = r#"{"answers":{"db":"Postgres"}}"#;

fn suspended(envelopes: &[Envelope]) -> (SuspendStatus, Vec<String>) {
    match &envelopes.last().unwrap().event {
        Event::Suspended(s) => (s.status, s.pending_request_ids.clone()),
        other => panic!("expected a suspended stream end, got {other:?}"),
    }
}

async fn start(agent: &Agent) -> (RunId, Vec<Envelope>) {
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    (run_id, finish(stream).await)
}

#[tokio::test]
async fn an_ask_suspends_the_run_with_its_questions() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, envelopes) = start(&agent).await;

    let tail = &kinds(&envelopes)[envelopes.len() - 2..];
    assert_eq!(tail, ["input_request", "suspended"]);
    let Event::InputRequest(request) = &envelopes[envelopes.len() - 2].event else {
        unreachable!()
    };
    assert_eq!(request.request_id, "ask_0");
    assert_eq!(request.kind, InputRequestKind::AskUser);
    assert_eq!(request.prompt, "Which database?");
    assert_eq!(request.questions[0].options[1].label, "Memory");
    let want = (SuspendStatus::AwaitingInput, vec!["ask_0".to_owned()]);
    assert_eq!(suspended(&envelopes), want);
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::AwaitingInput);
    assert!(matches!(
        record.checkpoint.phase,
        Phase::AwaitingInput { .. }
    ));
    assert!(!kinds(&envelopes).contains(&"tool_call_start".to_owned()));
}

#[tokio::test]
async fn an_answer_becomes_the_calls_result_and_the_run_completes() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, _) = start(&agent).await;
    let envelopes = finish(agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;

    let kinds = kinds(&envelopes);
    assert_eq!(kinds[..3], ["init", "tool_call_start", "tool_call_end"]);
    assert_eq!(kinds.last().unwrap(), "result");
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), [ANSWERED]);
    assert_eq!(log[3], Message::assistant_text("done"));
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}

#[tokio::test]
async fn a_declined_answer_tells_the_model_to_go_on() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, _) = start(&agent).await;
    let declined = HashMap::from([("ask_0".to_owned(), AskUserAnswer::declined("busy"))]);
    finish(agent.answer(run_id, declined, RunOptions::new("s1"))).await;
    let results = result_texts(&messages(&*stores, "s1").await[2]);
    assert!(
        results[0].starts_with("The user declined to answer."),
        "{results:?}"
    );
    assert!(results[0].ends_with("User note: busy"), "{results:?}");
}

#[tokio::test]
async fn a_wrong_answer_is_refused_and_the_run_stays_suspended() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, _) = start(&agent).await;
    let writes = stores.log.lock().unwrap().len();
    let many = AnswerValue::Many(vec!["Postgres".into()]);
    let wrong = [
        HashMap::new(),
        HashMap::from([("ask_9".to_owned(), postgres()["ask_0"].clone())]),
        HashMap::from([("ask_0".to_owned(), AskUserAnswer::new([]))]),
        HashMap::from([(
            "ask_0".to_owned(),
            AskUserAnswer::new([("db".into(), many)]),
        )]),
    ];
    for answers in wrong {
        let (envelopes, error) =
            drain(agent.answer(run_id.clone(), answers, RunOptions::new("s1"))).await;
        assert!(envelopes.is_empty());
        assert!(
            matches!(error, Some(RunError::InvalidAnswer { .. })),
            "{error:?}"
        );
    }
    assert_eq!(
        stores.log.lock().unwrap().len(),
        writes,
        "nothing was written"
    );
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::AwaitingInput);
    finish(agent.answer(run_id, postgres(), RunOptions::new("s1"))).await;
}

#[tokio::test]
async fn approve_and_answer_each_need_their_own_suspend() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &runs, stores.clone());
    let (run_id, _) = start(&agent).await;
    let allow = HashMap::from([("ask_0".to_owned(), ApprovalResponse::Allow)]);
    let (_, error) = drain(agent.approve(run_id.clone(), allow, RunOptions::new("s1"))).await;
    assert!(
        matches!(error, Some(RunError::NotSuspended { .. })),
        "{error:?}"
    );

    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 1, "Write"),
        text_turn("done"),
    ]));
    let agent = build(provider, &runs, stores);
    let (run_id, envelopes) = start(&agent).await;
    assert_eq!(suspended(&envelopes).0, SuspendStatus::AwaitingApproval);
    let (_, error) = drain(agent.answer(run_id, postgres(), RunOptions::new("s1"))).await;
    assert!(
        matches!(error, Some(RunError::NotSuspended { .. })),
        "{error:?}"
    );
}

#[tokio::test]
async fn a_resume_asks_again_and_a_late_answer_is_refused() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, _) = start(&agent).await;
    let again = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    assert_eq!(
        suspended(&again),
        (SuspendStatus::AwaitingInput, vec!["ask_0".to_owned()])
    );

    finish(agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;
    let (envelopes, error) =
        drain(agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;
    assert!(envelopes.is_empty());
    assert!(
        matches!(error, Some(RunError::Terminal { .. })),
        "{error:?}"
    );
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 4, "the answer is in the log once: {log:?}");
}

/// A call that needs approval and an `AskUser` call in one response: the
/// run asks for the approval first, then for the answer, and the approved
/// call runs once, after the answer.
#[tokio::test]
async fn approval_comes_before_the_question_and_nothing_runs_twice() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let provider = Arc::new(Replay::new(vec![
        ask_turn(0, 1, "Write"),
        text_turn("done"),
    ]));
    let agent = build(provider, &runs, stores.clone());
    let (run_id, envelopes) = start(&agent).await;
    let want = (SuspendStatus::AwaitingApproval, vec!["call_0_0".to_owned()]);
    assert_eq!(suspended(&envelopes), want);

    let allow = HashMap::from([("call_0_0".to_owned(), ApprovalResponse::Allow)]);
    let envelopes = finish(agent.approve(run_id.clone(), allow, RunOptions::new("s1"))).await;
    assert_eq!(
        suspended(&envelopes),
        (SuspendStatus::AwaitingInput, vec!["ask_0".to_owned()])
    );
    assert!(
        runs.lock().unwrap().is_empty(),
        "nothing ran before the answer"
    );

    finish(agent.answer(run_id, postgres(), RunOptions::new("s1"))).await;
    assert_eq!(runs.lock().unwrap()["call_0_0"], 1);
    let results = result_texts(&messages(&*stores, "s1").await[2]);
    assert_eq!(results, ["v0.0", ANSWERED]);
}

#[tokio::test]
async fn a_subagent_cannot_ask_the_user() {
    let parent = vec![spawn_turn(None, &["ask them"]), text_turn("done")];
    let child = vec![ask_turn(0, 0, ""), text_turn("assumed Postgres")];
    let family = Arc::new(Family::new(parent, child));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(family)
        .tools(tools(&Runs::default()))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(Vec::new())
        .build()
        .unwrap();
    let (run_id, envelopes) = start(&agent).await;
    assert_eq!(kinds(&envelopes).last().unwrap(), "result");
    let child_log = messages(&*stores, &format!("{run_id}:sub_0")).await;
    let refused = result_texts(&child_log[2]);
    assert!(
        refused[0].starts_with("AskUser is not available here"),
        "{refused:?}"
    );
    assert_eq!(
        result_texts(&messages(&*stores, "s1").await[2]),
        ["assumed Postgres"]
    );
}

/// A crash at any write of the answering call: the run completes with the
/// answer in its log exactly once, and an answer sent again after the
/// answer committed is refused.
#[tokio::test]
async fn a_crash_at_any_write_after_the_answer_replays_it_once() {
    let turns = || Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    // The writes of a clean answer, counted from the suspend.
    let clean = Arc::new(Stores::default());
    let agent = build(turns(), &Runs::default(), clean.clone());
    let (run_id, _) = start(&agent).await;
    let before = clean.log.lock().unwrap().len() as u64;
    finish(agent.answer(run_id, postgres(), RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    assert_eq!(result_texts(&want[2]), [ANSWERED]);
    let writes = clean.log.lock().unwrap().len() as u64 - before;
    assert!(writes >= 3, "claim, answer, results and terminal: {writes}");

    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("answer write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let (run_id, _) = start(&build(provider.clone(), &runs, stores.clone())).await;
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashed = build(provider.clone(), &runs, faulty);
            let (_, error) =
                drain(crashed.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;
            assert!(error.is_some(), "{context}");

            let agent = build(provider.clone(), &runs, stores.clone());
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            let answered = !matches!(record.checkpoint.phase, Phase::AwaitingInput { .. });
            if answered {
                // Committed: a second answer is refused, a resume goes on.
                let retry = agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"));
                let (_, error) = drain(retry).await;
                let refused = matches!(
                    error,
                    Some(RunError::NotSuspended { .. } | RunError::Terminal { .. })
                );
                assert!(refused, "{context}: {error:?}");
                if !record.status.is_terminal() {
                    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                }
            } else {
                finish(agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;
            }
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            assert_eq!(messages(&*stores, "s1").await, want, "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let ends = (events.iter())
                .filter(|e| matches!(&e.event, Event::ToolCallEnd(_)))
                .count();
            assert_eq!(ends, 1, "{context}: one answer");
            // A response whose receipt was lost is bought again, once.
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
}

/// A crash at any write of the run before the answer: the run is resumed
/// or answered, and the answer lands once.
#[tokio::test]
async fn a_crash_at_any_write_before_the_suspend_asks_once() {
    let turns = || Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let clean = Arc::new(Stores::default());
    let (_, _) = start(&build(turns(), &Runs::default(), clean.clone())).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = build(provider.clone(), &runs, faulty).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            let agent = build(provider.clone(), &runs, stores.clone());
            let run_id = match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status == RunStatus::AwaitingInput => run_id,
                Ok(_) => {
                    assert!(error.is_some(), "{context}");
                    let resumed = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                    assert_eq!(
                        suspended(&resumed).0,
                        SuspendStatus::AwaitingInput,
                        "{context}"
                    );
                    run_id
                }
                Err(_) => start(&agent).await.0,
            };
            finish(agent.answer(run_id.clone(), postgres(), RunOptions::new("s1"))).await;
            let log = messages(&*stores, "s1").await;
            assert_eq!(result_texts(&log[2]), [ANSWERED], "{context}");
            assert_eq!(log.len(), 4, "{context}");
        }
    }
}
