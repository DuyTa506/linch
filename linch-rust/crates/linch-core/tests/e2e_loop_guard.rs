//! The loop guard: a run that repeats one tool call, fails every call
//! batch after batch, or skips its provider call turn after turn ends
//! `failed` with a `loop_guard` event, even with `max_turns` unset; and a
//! crash at every write of a tripping run resumes to the same end.

mod common;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use linch_core::hooks::{BeforeProviderCall, BeforeProviderCallResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, Hooks, JsonObject, LoopGuard, Message,
    PermissionMode, Role, RunId, RunOptions, RunStatus, RunStore, ScriptedTurn, SessionStore, Tool,
    ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
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
/// `max_turns` unset, and with both tool-call checks off: 25 turns in a row
/// may end without a call, and the run ends at the next one, having paid
/// for nothing.
#[tokio::test]
async fn an_always_skipping_hook_ends_the_run_without_max_turns() {
    let mut checks_off = spec();
    checks_off.loop_guard = LoopGuard {
        max_identical_tool_calls: 0,
        max_consecutive_failures: 0,
        ..LoopGuard::default()
    };
    for spec in [spec(), checks_off] {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(Vec::new()));
        let agent = agent_on(
            spec,
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

/// A read tool that answers each call with `answer(n)`, `n` counting its
/// calls from 0; a poll when `poll`.
struct Ticker {
    name: &'static str,
    poll: bool,
    /// Whether each answer says the polled thing is done.
    done: bool,
    answer: fn(u32) -> String,
    calls: AtomicU32,
    schema: JsonObject,
}

impl Ticker {
    fn new(name: &'static str, poll: bool, answer: fn(u32) -> String) -> Arc<Self> {
        Arc::new(Self {
            name,
            poll,
            done: false,
            answer,
            calls: AtomicU32::new(0),
            schema: object(json!({"type": "object"})),
        })
    }

    /// A poll of a command that has ended: every read answers the same and
    /// says so, as `BashOutput` does.
    fn finished(name: &'static str) -> Arc<Self> {
        Arc::new(Self {
            name,
            poll: true,
            done: true,
            answer: |_| "[exit code 0]".into(),
            calls: AtomicU32::new(0),
            schema: object(json!({"type": "object"})),
        })
    }

    fn calls(&self) -> u32 {
        self.calls.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl Tool for Ticker {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Answer."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    fn polls(&self) -> bool {
        self.poll
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        let mut output = ToolOutput::new(json!((self.answer)(n)));
        if self.done {
            output
                .metadata
                .insert(linch_core::loop_guard::POLL_DONE.into(), true.into());
        }
        Ok(output)
    }
}

/// Ten calls of `tool` with the same input, then an answer.
fn ten_turns(tool: &str) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = (0..10)
        .map(|t| {
            TurnBuilder::new(MODEL)
                .tool_call(&format!("call_{t}"), tool, &json!({}))
                .build()
        })
        .collect();
    turns.push(text_turn("done"));
    turns
}

/// An agent with only `tool`, on `stores`, answered by `provider`.
fn ticking<S: RunStore + SessionStore + 'static>(
    tool: Arc<Ticker>,
    provider: Arc<Replay>,
    stores: Arc<S>,
) -> Agent {
    let tools = ToolRegistry::new();
    let _ = tools.register(tool).unwrap();
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(Arc::new(tools))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

/// [`ten_turns`] of `tool`, run to the end.
async fn ten_calls(tool: Arc<Ticker>) -> (RunStatus, Vec<Envelope>) {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(ten_turns(tool.name)));
    let stream = ticking(tool, provider, stores.clone()).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    ended(&stores, &run_id).await
}

/// A repeat is the same call with the same result: a call whose result
/// differs from every earlier identical call's made progress (a file read
/// again after an edit), however often it is made.
#[tokio::test]
async fn the_same_call_with_a_new_result_is_progress() {
    let changing = Ticker::new("Status", false, |n| format!("step {n}"));
    let (status, events) = ten_calls(changing.clone()).await;
    // No result repeats, so the repeat count never trips; the tenth call of
    // one input does (Python's rule, loosened), whatever the results.
    assert_eq!(status, RunStatus::Failed);
    let guard = guards(&events);
    let detail = guard[0]["detail"].as_str().unwrap();
    assert!(detail.contains("identical inputs 10 time(s)"), "{detail}");
    assert!(!detail.contains("same result"), "{detail}");
    assert_eq!(changing.calls(), 10);

    let same = Ticker::new("Status", false, |_| "idle".into());
    let (status, events) = ten_calls(same.clone()).await;
    assert_eq!(status, RunStatus::Failed);
    let guard = guards(&events);
    assert_eq!(guard[0]["reason"], "repeated_tool_call", "{guard:?}");
    assert_eq!(same.calls(), 3);

    // Back to an earlier answer is a repeat of that answer.
    let flipping = Ticker::new("Status", false, |n| format!("side {}", n % 2));
    let (status, _) = ten_calls(flipping.clone()).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(flipping.calls(), 5, "the third `side 0`");
}

/// A tool that declares itself a poll (`BashOutput` reading a command that
/// runs on) is waited on: ten identical answers are neither a repeat nor
/// ten identical inputs, since the idle bound is 25.
#[tokio::test]
async fn a_poll_is_waited_on_not_counted_as_a_repeat() {
    let poll = Ticker::new("Poll", true, |_| "still running".into());
    let (status, events) = ten_calls(poll.clone()).await;
    assert_eq!(status, RunStatus::Completed, "{:?}", guards(&events));
    assert_eq!(poll.calls(), 10);
}

/// The result that keys a repeat is read back from the durable
/// `tool_result` events: a crash at any write of a run whose answers
/// alternate ends it on the third durable copy of one answer, never
/// earlier. An interrupted call's placeholder is an error, which keys
/// nothing.
#[tokio::test]
async fn a_crash_at_any_write_keys_repeats_on_the_durable_results() {
    let flipping = || Ticker::new("Status", false, |n| format!("side {}", n % 2));
    let clean = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(ten_turns("Status")));
    finish(ticking(flipping(), provider, clean.clone()).run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, tool) = (Arc::new(Stores::default()), flipping());
            let provider = Arc::new(Replay::new(ten_turns("Status")));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashed = ticking(tool.clone(), provider.clone(), faulty);
            let stream = crashed.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = ticking(tool, provider, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;

            let (status, events) = ended(&stores, &run_id).await;
            assert_eq!(status, RunStatus::Failed, "{context}");
            let guard = guards(&events);
            assert_eq!(guard.len(), 1, "{context}: {guard:?}");
            assert_eq!(guard[0]["reason"], "repeated_tool_call", "{context}");
            let session = messages(&*stores, "s1").await;
            let answers: Vec<String> = (session.iter())
                .map(result_texts)
                .filter(|texts| !texts.is_empty())
                .map(|texts| texts[0].clone())
                .collect();
            let (last, before) = answers.split_last().unwrap();
            let copies = |text: &str, of: &[String]| of.iter().filter(|a| *a == text).count();
            assert_eq!(copies(last, &answers), 3, "{context}: {answers:?}");
            for answer in before {
                assert!(copies(answer, before) < 3, "{context}: {answers:?}");
            }
        }
    }
}

/// A poll that says its command is done is a repeat at the usual count, and
/// the `tool_call_end` that says so is read back from the durable log: a
/// crash at any write of such a run ends it on the third read, as a run
/// without a crash does, and a poll that does not say it is done is waited
/// on (`a_poll_is_waited_on_not_counted_as_a_repeat`).
#[tokio::test]
async fn a_crash_at_any_write_keeps_a_finished_poll_a_repeat() {
    let finished = || Ticker::new("Wait", true, |_| "[exit code 0]".into());
    let done = || Ticker::finished("Wait");
    let (status, events) = ten_calls(finished()).await;
    assert_eq!(status, RunStatus::Completed, "{:?}", guards(&events));

    let (status, events) = ten_calls(done()).await;
    assert_eq!(status, RunStatus::Failed);
    assert_eq!(guards(&events)[0]["reason"], "repeated_tool_call");

    let clean = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(ten_turns("Wait")));
    finish(ticking(done(), provider, clean.clone()).run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, tool) = (Arc::new(Stores::default()), done());
            let provider = Arc::new(Replay::new(ten_turns("Wait")));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let crashed = ticking(tool.clone(), provider.clone(), faulty);
            let stream = crashed.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = ticking(tool, provider, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;

            let (status, events) = ended(&stores, &run_id).await;
            assert_eq!(status, RunStatus::Failed, "{context}");
            let guard = guards(&events);
            assert_eq!(guard.len(), 1, "{context}: {guard:?}");
            assert_eq!(guard[0]["reason"], "repeated_tool_call", "{context}");
        }
    }
}
