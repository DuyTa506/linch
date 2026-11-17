//! The loop guard: a run that repeats one tool call, fails every call
//! batch after batch, or skips its provider call turn after turn ends
//! `failed` with a `loop_guard` event, even with `max_turns` unset; and a
//! crash at every write of a tripping run resumes to the same end.

mod common;

use std::sync::Arc;

use common::hooks::hook;
use common::*;
use linch_core::hooks::{BeforeProviderCall, BeforeProviderCallResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, Hooks, Message, PermissionMode, Role, RunId,
    RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, ToolScope,
};
use serde_json::{Value, json};

/// A response calling `tool` once with the input `{"value": value}`.
fn call(turn: usize, tool: &str, value: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text("working")
        .tool_call(&format!("call_{turn}"), tool, &json!({"value": value}))
        .usage(usage(10, 5))
        .build()
}

/// Ten responses calling `Read` with the same input.
fn same_call() -> Vec<ScriptedTurn> {
    (0..10).map(|turn| call(turn, "Read", "same")).collect()
}

fn agent_on<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    hooks: Hooks,
    stores: Arc<S>,
    runs: &Runs,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

/// A hook that skips every provider call.
fn always_skip() -> Hooks {
    let hooks = Hooks::default();
    let again = hook::<BeforeProviderCall>("again", |_| {
        Ok(BeforeProviderCallResponse::ForceContinue {
            feedback: "again".into(),
        })
    });
    let _ = hooks.before_provider_call.register(again.arc()).unwrap();
    hooks
}

/// A hook that lets the first provider call through, then skips every
/// later one.
fn skip_after_first() -> Hooks {
    let hooks = Hooks::default();
    let later = hook::<BeforeProviderCall>("later", |request| {
        let answered = (request.request.messages.iter()).any(|m| m.role == Role::Assistant);
        Ok(match answered {
            false => BeforeProviderCallResponse::Continue,
            true => BeforeProviderCallResponse::ForceContinue {
                feedback: "again".into(),
            },
        })
    });
    let _ = hooks.before_provider_call.register(later.arc()).unwrap();
    hooks
}

/// The wire form of every `loop_guard` event.
fn guards(events: &[Envelope]) -> Vec<Value> {
    (events.iter())
        .filter(|envelope| matches!(envelope.event, Event::LoopGuard(_)))
        .map(|envelope| serde_json::to_value(&envelope.event).unwrap())
        .collect()
}

/// The `name` of every `error` event.
fn errors(events: &[Envelope]) -> Vec<String> {
    (events.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Error(error) => Some(error.error["name"].as_str()?.to_owned()),
            _ => None,
        })
        .collect()
}

async fn ended(stores: &Stores, run_id: &RunId) -> (RunStatus, Vec<Envelope>) {
    let record = stores.runs.load(NS, run_id).await.unwrap();
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    (record.status, events)
}

/// Python's `LoopGuard` default: the third call with the same input runs,
/// then the run stops before calling the provider again. The session ends
/// with that call's result, so it stays paired.
#[tokio::test]
async fn the_third_identical_call_ends_the_run() {
    let stores = Arc::new(Stores::default());
    let (provider, runs) = (Arc::new(Replay::new(same_call())), Runs::default());
    let agent = agent_on(
        spec(),
        provider.clone(),
        Hooks::default(),
        stores.clone(),
        &runs,
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;

    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    let guard = guards(&events);
    assert_eq!(guard.len(), 1, "{guard:?}");
    assert_eq!(guard[0]["reason"], "repeated_tool_call");
    assert_eq!(guard[0]["action"], "stop");
    assert_eq!(errors(&events), ["LoopGuardError"]);
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
    assert_eq!(runs.lock().unwrap().len(), 3, "the third call ran");
    let session = messages(&*stores, "s1").await;
    assert_eq!(session.len(), 7, "{session:?}");
    assert_eq!(session[6].role, Role::User);
    let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(
        serde_json::to_value(result).unwrap()["stop_reason"],
        "error"
    );
}

/// Three batches in a row where every call failed end the run; a batch with
/// a call that succeeded starts the count again.
#[tokio::test]
async fn three_failed_batches_in_a_row_end_the_run() {
    let failing: Vec<_> = (0..10)
        .map(|t| call(t, "Missing", &t.to_string()))
        .collect();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(failing));
    let agent = agent_on(
        spec(),
        provider.clone(),
        Hooks::default(),
        stores.clone(),
        &Runs::default(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    let guard = guards(&events);
    assert_eq!(guard.len(), 1, "{guard:?}");
    assert_eq!(guard[0]["reason"], "repeated_failures");
    assert_eq!(provider.calls().values().sum::<u32>(), 3);

    let mixed = vec![
        call(0, "Missing", "a"),
        call(1, "Missing", "b"),
        call(2, "Read", "c"),
        call(3, "Missing", "d"),
        call(4, "Missing", "e"),
        text_turn("done"),
    ];
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(mixed));
    let agent = agent_on(
        spec(),
        provider,
        Hooks::default(),
        stores.clone(),
        &Runs::default(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Completed, "{:?}", guards(&events));
}

/// A call that fails is not counted as a repeat: the failure streak ends a
/// run that repeats one failing call. A refused call the model is told to
/// repeat (a deferred tool) therefore never trips the repeat check.
#[tokio::test]
async fn an_identical_failing_call_is_the_failure_streaks() {
    let failing: Vec<_> = (0..10).map(|t| call(t, "Missing", "same")).collect();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(failing));
    let agent = agent_on(
        spec(),
        provider.clone(),
        Hooks::default(),
        stores.clone(),
        &Runs::default(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    let guard = guards(&events);
    assert_eq!(guard.len(), 1, "{guard:?}");
    assert_eq!(guard[0]["reason"], "repeated_failures");
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
}

/// A `before_provider_call` hook that always skips the call is bounded with
/// `max_turns` unset: 25 turns in a row may end without a call, and the run
/// ends at the next one, having paid for nothing.
#[tokio::test]
async fn an_always_skipping_hook_ends_the_run_without_max_turns() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(Vec::new()));
    let agent = agent_on(
        spec(),
        provider.clone(),
        always_skip(),
        stores.clone(),
        &Runs::default(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;

    let (status, events) = ended(&stores, &run_id).await;
    assert_eq!(status, RunStatus::Failed);
    assert!(provider.calls().is_empty());
    let guard = guards(&events);
    assert_eq!(guard.len(), 1, "{guard:?}");
    assert_eq!(guard[0]["reason"], "idle_turns");
    assert_eq!(errors(&events), ["LoopGuardError"]);
    let session = messages(&*stores, "s1").await;
    assert_eq!(session.len(), 1 + 25, "the prompt and 25 skipped turns");
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

/// The guard's count lives in the durable log, not in the process: a crash
/// at any write of a run the third identical call stops, before or after
/// the write lands, resumes to the same end. No call runs twice, and no
/// turn is paid for past the third, or the fourth when a crash left the
/// interrupted placeholder of a call (an error, which does not count) in
/// place of its result.
#[tokio::test]
async fn a_crash_at_any_write_of_an_identical_call_loop_still_trips() {
    let clean = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(same_call()));
    let agent = agent_on(
        spec(),
        provider,
        Hooks::default(),
        clean.clone(),
        &Runs::default(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
            let provider = Arc::new(Replay::new(same_call()));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = agent_on(spec(), provider.clone(), Hooks::default(), faulty, &runs)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = agent_on(
                spec(),
                provider.clone(),
                Hooks::default(),
                stores.clone(),
                &runs,
            );
            let run_id = recover(&agent, &stores, run_id).await;

            let (status, events) = ended(&stores, &run_id).await;
            assert_eq!(status, RunStatus::Failed, "{context}");
            let guard = guards(&events);
            assert_eq!(guard.len(), 1, "{context}: {guard:?}");
            assert_eq!(guard[0]["reason"], "repeated_tool_call", "{context}");
            let calls = provider.calls();
            assert!(calls.keys().all(|&turn| turn <= 3), "{context}: {calls:?}");
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
            let ran = runs.lock().unwrap().clone();
            assert!(ran.values().all(|&n| n == 1), "{context}: {ran:?}");
            let session = messages(&*stores, "s1").await;
            let interrupted = (session.iter()).any(|message| {
                result_texts(message) == [linch_core::journal::INTERRUPTED_TOOL_TEXT]
            });
            let turns = if interrupted { 4 } else { 3 };
            let counted = (session.iter())
                .map(result_texts)
                .filter(|texts| !texts.is_empty())
                .filter(|texts| texts != &[linch_core::journal::INTERRUPTED_TOOL_TEXT])
                .count();
            assert_eq!(counted, 3, "{context}: three results counted");
            assert_eq!(session.len(), 1 + 2 * turns, "{context}: {session:?}");
        }
    }
}

/// The same for the skipped-call bound: the count of turns without a call
/// is read back from the durable deliveries and the checkpoint's turn, so a
/// crash cannot reset it and let the hook loop on, nor move it. One call
/// is made first, so the count starts after its turn.
#[tokio::test]
async fn a_crash_at_any_write_of_a_skip_loop_still_trips() {
    let first = || Arc::new(Replay::new(vec![tool_turn(0, 1, "Read")]));
    let clean = Arc::new(Stores::default());
    let agent = agent_on(
        spec(),
        first(),
        skip_after_first(),
        clean.clone(),
        &Runs::default(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    assert_eq!(
        want.len(),
        3 + 25,
        "the call, its result and 25 skipped turns"
    );
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
            let provider = first();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = agent_on(spec(), provider.clone(), skip_after_first(), faulty, &runs)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let hooks = skip_after_first();
            let agent = agent_on(spec(), provider.clone(), hooks, stores.clone(), &runs);
            let run_id = recover(&agent, &stores, run_id).await;

            let (status, events) = ended(&stores, &run_id).await;
            assert_eq!(status, RunStatus::Failed, "{context}");
            let guard = guards(&events);
            assert_eq!(guard.len(), 1, "{context}: {guard:?}");
            assert_eq!(guard[0]["reason"], "idle_turns", "{context}");
            let calls = provider.calls();
            assert!(calls.keys().all(|&turn| turn == 0), "{context}: {calls:?}");
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
            // A crash right after the tool started leaves its interrupted
            // placeholder in place of its result; the rest is the same.
            let got: Vec<Message> = messages(&*stores, "s1").await;
            assert_eq!(got.len(), want.len(), "{context}");
            assert_eq!(got[..2], want[..2], "{context}");
            assert_eq!(got[3..], want[3..], "{context}");
        }
    }
}
