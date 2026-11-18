//! `AskUser`: a call suspends the run for the user's answer, `answer`
//! commits it as the call's result and continues the run, and a crash at
//! any write replays the answer exactly once.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::hooks::hook;
use common::subagents::*;
use common::*;
use linch_core::ask_user::{AnswerValue, AskUser, AskUserAnswer};
use linch_core::events::kinds::{InputRequestKind, SuspendStatus};
use linch_core::hooks::{ApprovalResponse, PreToolUse, PreToolUseResponse};
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, Hooks, Message, PermissionMode, Phase, RunError, RunId,
    RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, Tool, ToolOutput, ToolRegistry,
    ToolScope,
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

/// Two `AskUser` calls in one response: an answer to one leaves the run
/// waiting for the other only, then the second answer completes it.
#[tokio::test]
async fn a_request_left_out_of_an_answer_is_asked_again() {
    let stores = Arc::new(Stores::default());
    let two = TurnBuilder::new(MODEL)
        .tool_call("ask_a", "AskUser", &question())
        .tool_call("ask_b", "AskUser", &question())
        .usage(usage(10, 5))
        .build();
    let provider = Arc::new(Replay::new(vec![two, text_turn("done")]));
    let agent = build(provider, &Runs::default(), stores.clone());
    let (run_id, envelopes) = start(&agent).await;
    let both = vec!["ask_a".to_owned(), "ask_b".to_owned()];
    assert_eq!(suspended(&envelopes), (SuspendStatus::AwaitingInput, both));

    let memory = AskUserAnswer::new([("db".to_owned(), AnswerValue::One("Memory".into()))]);
    let first = HashMap::from([("ask_a".to_owned(), memory)]);
    let again = finish(agent.answer(run_id.clone(), first, RunOptions::new("s1"))).await;
    let only_b = (SuspendStatus::AwaitingInput, vec!["ask_b".to_owned()]);
    assert_eq!(suspended(&again), only_b);
    let asked: Vec<&str> = (again.iter())
        .filter_map(|e| match &e.event {
            Event::InputRequest(request) => Some(request.request_id.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(asked, ["ask_b"]);

    let second = HashMap::from([("ask_b".to_owned(), postgres()["ask_0"].clone())]);
    finish(agent.answer(run_id, second, RunOptions::new("s1"))).await;
    let results = result_texts(&messages(&*stores, "s1").await[2]);
    assert_eq!(results, [r#"{"answers":{"db":"Memory"}}"#, ANSWERED]);
}

/// A `pre_tool_use` hook that resolves an `AskUser` call answers it: the
/// run does not suspend.
#[tokio::test]
async fn a_hook_can_answer_for_the_user() {
    let hooks = Hooks::default();
    let answer = hook::<PreToolUse>("auto_answer", |request| {
        Ok(match request.tool_name.as_str() {
            "AskUser" => PreToolUseResponse::Resolve {
                result: Ok(ToolOutput::new(ANSWERED)),
            },
            _ => PreToolUseResponse::Continue,
        })
    });
    let _ = hooks.pre_tool_use.register(answer.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let agent = AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools(&Runs::default()))
        .permissions(policy(PermissionMode::Default))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let (_, envelopes) = start(&agent).await;
    assert_eq!(kinds(&envelopes).last().unwrap(), "result");
    assert_eq!(result_texts(&messages(&*stores, "s1").await[2]), [ANSWERED]);
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

/// Drive `run_id` to its end after a crash: answer it while it awaits
/// input, else resume it. An answer that already committed is refused.
async fn recover(agent: &Agent, stores: &Stores, run_id: &RunId, context: &str) {
    for _ in 0..4 {
        let record = stores.runs.load(NS, run_id).await.unwrap();
        if record.status.is_terminal() {
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            return;
        }
        let options = RunOptions::new("s1");
        let stream = match record.checkpoint.phase {
            Phase::AwaitingInput { .. } => agent.answer(run_id.clone(), postgres(), options),
            _ => agent.resume(run_id.clone(), options),
        };
        let (_, error) = drain(stream).await;
        assert!(error.is_none(), "{context}: {error:?}");
    }
    panic!("{context}: the run did not end");
}

/// One response asks the user and calls a write tool, which waits with the
/// question. A crash at any write, from the start to the end of the
/// answered run, then recovery: the write tool runs exactly once and the
/// log is the clean run's, except at the two writes around the tool itself,
/// where it is reported interrupted (run at most once, never twice).
#[tokio::test]
async fn a_crash_at_any_write_of_a_mixed_batch_runs_the_other_tool_once() {
    let turns = || {
        Arc::new(Replay::new(vec![
            ask_turn(0, 1, "Write"),
            text_turn("done"),
        ]))
    };
    fn build<S: RunStore + SessionStore + 'static>(
        provider: Arc<Replay>,
        runs: &Runs,
        stores: Arc<S>,
    ) -> Agent {
        let skip = policy(PermissionMode::SkipDangerous);
        agent(provider, tools(runs), skip, stores)
    }
    let clean = Arc::new(Stores::default());
    let runs = Runs::default();
    let agent = build(turns(), &runs, clean.clone());
    let (run_id, envelopes) = start(&agent).await;
    let pending = (SuspendStatus::AwaitingInput, vec!["ask_0".to_owned()]);
    assert_eq!(
        suspended(&envelopes),
        pending,
        "the write waits with the question"
    );
    assert!(runs.lock().unwrap().is_empty());
    finish(agent.answer(run_id, postgres(), RunOptions::new("s1"))).await;
    assert_eq!(runs.lock().unwrap()["call_0_0"], 1);
    let want = messages(&*clean, "s1").await;
    assert_eq!(result_texts(&want[2]), ["v0.0", ANSWERED]);
    let writes = clean.log.lock().unwrap().len() as u64;

    let mut interrupted = Vec::new();
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashed = build(provider.clone(), &runs, faulty);
            let stream = crashed.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            if error.is_none() {
                let answer = crashed.answer(run_id.clone(), postgres(), RunOptions::new("s1"));
                let (_, error) = drain(answer).await;
                assert!(error.is_some(), "{context}: the fault hit no write");
            }
            let agent = build(provider.clone(), &runs, stores.clone());
            let run_id = match stores.runs.load(NS, &run_id).await {
                Ok(_) => run_id,
                // The create failed: nothing exists, so start over.
                Err(_) => start(&agent).await.0,
            };
            recover(&agent, &stores, &run_id, &context).await;

            let ran = runs.lock().unwrap().get("call_0_0").copied().unwrap_or(0);
            let log = messages(&*stores, "s1").await;
            match result_texts(&log[2]).as_slice() {
                [first, answer] if first == INTERRUPTED_TOOL_TEXT => {
                    assert_eq!(answer, ANSWERED, "{context}");
                    interrupted.push((n, timing, ran));
                }
                _ => {
                    assert_eq!(ran, 1, "{context}");
                    assert_eq!(log, want, "{context}");
                }
            }
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
    // At most once, as for any tool: only a crash between the write tool's
    // durable start and its durable end leaves it interrupted. Either its
    // start's ack was lost (it never ran) or its end was (it ran once).
    let [
        (start, FaultTiming::After, 0),
        (end, FaultTiming::Before, 1),
    ] = interrupted[..]
    else {
        panic!("interrupted only around the write tool: {interrupted:?}");
    };
    assert_eq!(end, start + 1, "{interrupted:?}");
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

/// An answer is the call's result, so the spec's tool-result redaction
/// scrubs it before its durable `tool_call_end` and the session.
#[tokio::test]
async fn an_answer_is_redacted_like_a_tool_result() {
    use linch_core::redaction::{RedactionRule, RedactionSpec};
    let mut spec = spec();
    spec.redaction = Some(RedactionSpec {
        rules: vec![RedactionRule::new(r"sk-[A-Za-z0-9]{8,}")],
        response: false,
        tool_results: true,
        prompt: false,
    });
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![ask_turn(0, 0, ""), text_turn("done")]));
    let runs = Runs::default();
    let permissions = policy(PermissionMode::Default);
    let agent = agent_with(spec, provider, tools(&runs), permissions, stores.clone());
    let (run_id, _) = start(&agent).await;
    let note = AskUserAnswer::declined("my key is sk-live0123456789");
    let declined = HashMap::from([("ask_0".to_owned(), note)]);
    let envelopes = finish(agent.answer(run_id.clone(), declined, RunOptions::new("s1"))).await;
    let results = result_texts(&messages(&*stores, "s1").await[2]);
    assert!(
        results[0].ends_with("User note: my key is [REDACTED]"),
        "{results:?}"
    );
    let stored = stores.runs.events(NS, &run_id, 0).await.unwrap();
    for events in [&envelopes, &stored] {
        let text = serde_json::to_string(events).unwrap();
        assert!(!text.contains("sk-live0123456789"), "{text}");
    }
}
