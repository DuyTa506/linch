//! Hooks that ask for another turn: an `after_provider_call` retry, which
//! accepts the paid response but sends feedback back instead of acting on
//! it, a `before_provider_call` `force_continue`, which skips the call and
//! sends feedback instead, and a crash at every write of either.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{HookOutcome, UserEventSubtype};
use linch_core::events::model_call::CallDisposition;
use linch_core::hooks::{
    AfterProviderCall, AfterProviderCallResponse, BeforeProviderCall, BeforeProviderCallResponse,
    Stop, StopResponse,
};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, ContentBlock, Envelope, Event, HookPoint, Hooks, Message, PermissionMode,
    Role, RunOptions, RunStatus, RunStore, SessionStore, ToolResultBlock, ToolScope,
};

fn hooked<S: RunStore + SessionStore + 'static>(
    provider: Arc<Replay>,
    hooks: Hooks,
    stores: Arc<S>,
    runs: &Runs,
) -> Agent {
    AgentBuilder::new(spec())
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

/// Asks for another turn whenever a response calls a tool, keyed on the
/// response alone so a resumed run's hook answers the same.
fn no_tools() -> Hooks {
    let hooks = Hooks::default();
    let judge = hook::<AfterProviderCall>("judge", |request| {
        Ok(match request.assembly.message.tool_uses().next() {
            Some(_) => AfterProviderCallResponse::Retry {
                feedback: "answer from what you know".into(),
            },
            None => AfterProviderCallResponse::Continue,
        })
    });
    let _ = hooks.after_provider_call.register(judge.arc()).unwrap();
    hooks
}

fn reminder(text: &str) -> String {
    format!("<system-reminder>\n{text}\n</system-reminder>")
}

fn tool_results(message: &Message) -> Vec<(String, bool)> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::ToolResult(result) => Some((result_text(result), result.is_error)),
            _ => None,
        })
        .collect()
}

fn result_text(result: &ToolResultBlock) -> String {
    result_texts(&Message::new(
        Role::User,
        vec![ContentBlock::ToolResult(result.clone())],
    ))
    .remove(0)
}

fn verdicts(envelopes: &[Envelope], point: HookPoint) -> Vec<String> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Hook(record) if record.point == point => match &record.outcome {
                HookOutcome::Answered { verdict } => Some(verdict.clone()),
                other => Some(format!("{other:?}")),
            },
            _ => None,
        })
        .collect()
}

/// A text-only response the hook retries is kept and logged; the model is
/// told the feedback in a system reminder, answers again, and only that
/// second answer reaches `stop`.
#[tokio::test]
async fn a_retried_answer_is_logged_and_the_model_answers_again() {
    let hooks = Hooks::default();
    let judge = hook::<AfterProviderCall>("judge", |request| {
        Ok(match request.assembly.message.text().as_str() {
            "first" => AfterProviderCallResponse::Retry {
                feedback: "cite a source".into(),
            },
            _ => AfterProviderCallResponse::Continue,
        })
    });
    let _ = hooks.after_provider_call.register(judge.arc()).unwrap();
    let stopped = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&stopped);
    let stop = hook::<Stop>("watch", move |request| {
        seen.lock().unwrap().push(request.text.clone());
        Ok(StopResponse::Allow)
    });
    let _ = hooks.stop.register(stop.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("first"), text_turn("second")]));
    let agent = hooked(provider.clone(), hooks, stores.clone(), &Runs::default());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let feedback = Message::user_text(reminder("cite a source"));
    let want = [
        Message::user_text("go"),
        Message::assistant_text("first"),
        feedback.clone(),
        Message::assistant_text("second"),
    ];
    assert_eq!(messages(&*stores, "s1").await, want);
    let notified = envelopes.iter().any(|e| {
        matches!(&e.event, Event::User(user)
            if user.subtype == UserEventSubtype::Notification && user.message == feedback)
    });
    assert!(notified, "the feedback is a durable notification");
    assert_eq!(
        verdicts(&envelopes, HookPoint::AfterProviderCall),
        ["retry", "continue"]
    );
    assert_eq!(
        *stopped.lock().unwrap(),
        ["second"],
        "the retried answer never stops"
    );
    assert_eq!(provider.calls().values().sum::<u32>(), 2);
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.final_text.as_deref(), Some("second"));
}

/// The tool calls of a retried response never run: each gets an error
/// result, the first with the feedback, so the provider history stays
/// paired.
#[tokio::test]
async fn a_retried_response_runs_none_of_its_tool_calls() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 2, "Read"),
        text_turn("done"),
    ]));
    let agent = hooked(provider, no_tools(), stores.clone(), &runs);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    assert!(runs.lock().unwrap().is_empty(), "no tool ran");
    assert!(!kinds(&envelopes).contains(&"tool_call_start".to_owned()));
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 4, "{log:?}");
    assert_eq!(
        log[1].tool_uses().count(),
        2,
        "the response is logged as given"
    );
    let not_run = "Tool not executed; revise and resubmit your final answer.";
    assert_eq!(
        tool_results(&log[2]),
        [
            ("answer from what you know".to_owned(), true),
            (not_run.to_owned(), true),
        ]
    );
    assert_eq!(log[3], Message::assistant_text("done"));
}

/// No retry budget bounds an `after_provider_call` retry, as in Python:
/// only `max_turns` does, and every retried answer is paid and logged.
#[tokio::test]
async fn an_after_provider_call_retry_is_bounded_only_by_max_turns() {
    let hooks = Hooks::default();
    let judge = hook::<AfterProviderCall>("judge", |_| {
        Ok(AfterProviderCallResponse::Retry {
            feedback: "again".into(),
        })
    });
    let _ = hooks.after_provider_call.register(judge.arc()).unwrap();
    let mut spec = spec();
    spec.max_turns = Some(3);
    let stores = Arc::new(Stores::default());
    let turns = (1..=4).map(|i| text_turn(&format!("answer {i}")));
    let provider = Arc::new(Replay::new(turns.collect()));
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
    assert_eq!(
        verdicts(&envelopes, HookPoint::AfterProviderCall),
        ["retry"; 3]
    );
    assert_eq!(
        messages(&*stores, "s1").await.len(),
        7,
        "go, then 3 answers and 3 feedbacks"
    );
    assert!(kinds(&envelopes).contains(&"loop_guard".to_owned()));
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

/// The accepted receipts an `assistant` event answered, and the orphans:
/// accepted receipts written before the `after_provider_call` hooks, with
/// no `assistant` event before the next delivery, because the run crashed
/// in between. An orphan keeps the `accepted` label it was written with
/// (the log is append-only); it is paid, counted, and its call went again.
fn accepted_receipts(envelopes: &[Envelope]) -> (usize, usize) {
    let (mut answered, mut orphans, mut open) = (0, 0, false);
    for envelope in envelopes {
        match &envelope.event {
            Event::ModelCallReceipt(receipt)
                if receipt.disposition == CallDisposition::Accepted =>
            {
                orphans += usize::from(open);
                open = true;
            }
            Event::Assistant(_) if open => {
                answered += 1;
                open = false;
            }
            Event::ModelCallDelivery(_) if open => {
                orphans += 1;
                open = false;
            }
            _ => {}
        }
    }
    (answered, orphans + usize::from(open))
}

/// The retry decision is durable with the paid response: a crash at any
/// write replays it exactly once and never runs the rejected tool call. A
/// response is bought again only after a crash between its receipt and
/// its write (an orphan), and never twice.
#[tokio::test]
async fn a_crash_at_any_write_of_a_retried_turn_replays_the_decision_once() {
    let turns = || {
        Arc::new(Replay::new(vec![
            tool_turn(0, 1, "Read"),
            text_turn("done"),
        ]))
    };
    let clean = Arc::new(Stores::default());
    let agent = hooked(turns(), no_tools(), clean.clone(), &Runs::default());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    assert_eq!(want.len(), 4, "{want:?}");
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = hooked(provider.clone(), no_tools(), faulty, &runs)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = hooked(provider.clone(), no_tools(), stores.clone(), &runs);
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
            assert!(runs.lock().unwrap().is_empty(), "{context}: a tool ran");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let (answered, orphans) = accepted_receipts(&events);
            assert_eq!(answered, 2, "{context}: one per turn");
            assert!(orphans <= 1, "{context}: {orphans} orphans");
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
}

/// Skips the call while the request holds fewer than two copies of its
/// feedback, keyed on the request alone so a resumed run's hook answers
/// the same: two turns in a row end without a call, with the same
/// feedback each time.
fn plan_first() -> Hooks {
    let hooks = Hooks::default();
    let plan = hook::<BeforeProviderCall>("plan", |request| {
        let note = Message::user_text(reminder("plan first"));
        let sent = (request.request.messages.iter())
            .filter(|message| **message == note)
            .count();
        Ok(match sent {
            0 | 1 => BeforeProviderCallResponse::ForceContinue {
                feedback: "plan first".into(),
            },
            _ => BeforeProviderCallResponse::Continue,
        })
    });
    let _ = hooks.before_provider_call.register(plan.arc()).unwrap();
    hooks
}

/// The turns the run's durable deliveries were for.
fn delivered_turns(envelopes: &[Envelope]) -> Vec<u64> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::ModelCallDelivery(delivery) => delivery.turn_index,
            _ => None,
        })
        .collect()
}

/// `force_continue` at `before_provider_call` ends the turn without a
/// call: the feedback is logged in a system reminder, as a durable
/// notification, and the next turn's call carries it. Python's
/// `_gate_retry_tail` (`src/linch/loop/runner.py:784-791`).
#[tokio::test]
async fn a_before_provider_call_force_continue_skips_the_call() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = hooked(
        provider.clone(),
        plan_first(),
        stores.clone(),
        &Runs::default(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let note = Message::user_text(reminder("plan first"));
    let want = [
        Message::user_text("go"),
        note.clone(),
        note.clone(),
        Message::assistant_text("done"),
    ];
    assert_eq!(messages(&*stores, "s1").await, want);
    let notified = (envelopes.iter())
        .filter(|e| {
            matches!(&e.event, Event::User(user)
                if user.subtype == UserEventSubtype::Notification && user.message == note)
        })
        .count();
    assert_eq!(notified, 2, "each feedback is a durable notification");
    assert_eq!(
        verdicts(&envelopes, HookPoint::BeforeProviderCall),
        ["force_continue", "force_continue", "continue"]
    );
    assert_eq!(provider.calls().values().sum::<u32>(), 1);
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    assert_eq!(
        delivered_turns(&events),
        [2],
        "no delivery for a skipped turn"
    );
    let builds = (kinds(&events).iter())
        .filter(|kind| *kind == "context_build")
        .count();
    assert_eq!(builds, 1, "a skipped turn records no context_build");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.final_text.as_deref(), Some("done"));
}

/// Only `max_turns` bounds an always-`force_continue` hook, as in Python:
/// the run ends failed with a `loop_guard` and never calls the provider.
#[tokio::test]
async fn an_always_force_continue_hook_ends_at_max_turns() {
    let hooks = Hooks::default();
    let again = hook::<BeforeProviderCall>("again", |_| {
        Ok(BeforeProviderCallResponse::ForceContinue {
            feedback: "again".into(),
        })
    });
    let _ = hooks.before_provider_call.register(again.arc()).unwrap();
    let mut spec = spec();
    spec.max_turns = Some(3);
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(Vec::new()));
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert!(provider.calls().is_empty(), "{:?}", provider.calls());
    assert_eq!(
        verdicts(&envelopes, HookPoint::BeforeProviderCall),
        ["force_continue"; 3]
    );
    let note = Message::user_text(reminder("again"));
    let want = [Message::user_text("go"), note.clone(), note.clone(), note];
    assert_eq!(messages(&*stores, "s1").await, want);
    assert!(kinds(&envelopes).contains(&"loop_guard".to_owned()));
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

/// A skipped turn's feedback is durable before the next turn: a crash at
/// any write, before or after it lands, resumes to the clean run's
/// session, with no delivery for a skipped turn. Only the one real call
/// can be bought again (a crash between its delivery and its answer).
#[tokio::test]
async fn a_crash_at_any_write_of_a_skipped_turn_resumes_without_a_call() {
    let turns = || Arc::new(Replay::new(vec![text_turn("done")]));
    let clean = Arc::new(Stores::default());
    let agent = hooked(turns(), plan_first(), clean.clone(), &Runs::default());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    assert_eq!(want.len(), 4, "{want:?}");
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = hooked(provider.clone(), plan_first(), faulty, &runs)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = hooked(provider.clone(), plan_first(), stores.clone(), &runs);
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
            let delivered = delivered_turns(&events);
            assert!(
                !delivered.is_empty() && delivered.iter().all(|&turn| turn == 2),
                "{context}: deliveries for turns {delivered:?}"
            );
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
}

/// Skips the call once the tool results are in, until the feedback is;
/// records the turn of every request it sees in `asked`.
fn after_results(asked: &Arc<Mutex<Vec<u32>>>) -> Hooks {
    let hooks = Hooks::default();
    let seen = Arc::clone(asked);
    let plan = hook::<BeforeProviderCall>("plan", move |request| {
        seen.lock().unwrap().push(request.turn);
        let note = Message::user_text(reminder("plan first"));
        let messages = &request.request.messages;
        let results = messages.iter().any(|m| {
            m.content
                .iter()
                .any(|block| matches!(block, ContentBlock::ToolResult(_)))
        });
        Ok(match results && !messages.contains(&note) {
            true => BeforeProviderCallResponse::ForceContinue {
                feedback: "plan first".into(),
            },
            false => BeforeProviderCallResponse::Continue,
        })
    });
    let _ = hooks.before_provider_call.register(plan.arc()).unwrap();
    hooks
}

/// Where Python and Rust part: Python fires `before_provider_call` on a
/// resumed response whose tool calls have no results yet, and a
/// `force_continue` there answers them with the feedback
/// (`src/linch/loop/runner.py:767-783`). A Rust resume of that response
/// goes straight on to its tool calls and never comes back to this point,
/// so the calls run and the hook first sees the turn after them.
#[tokio::test]
async fn a_resumed_response_runs_its_tool_calls_before_the_hook_fires() {
    let turns = || {
        Arc::new(Replay::new(vec![
            tool_turn(0, 1, "Read"),
            text_turn("done"),
        ]))
    };
    let clean = Arc::new(Stores::default());
    let asked = Arc::new(Mutex::new(Vec::new()));
    let agent = hooked(
        turns(),
        after_results(&asked),
        clean.clone(),
        &Runs::default(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let received = (clean.log.lock().unwrap().iter())
        .position(
            |write| matches!(write, Write::Commit(kinds) if kinds.iter().any(|k| k == "assistant")),
        )
        .expect("the response's write") as u64
        + 1;

    let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
    let asked = Arc::new(Mutex::new(Vec::new()));
    let faulty = Arc::new(Faulty::new(stores.clone(), received, FaultTiming::After));
    let stream = hooked(provider.clone(), after_results(&asked), faulty, &runs)
        .run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    assert!(drain(stream).await.1.is_some());
    assert!(
        runs.lock().unwrap().is_empty(),
        "the crash came before the tool"
    );
    let agent = hooked(
        provider.clone(),
        after_results(&asked),
        stores.clone(),
        &runs,
    );
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    assert_eq!(runs.lock().unwrap().len(), 1, "the resumed tool call ran");
    assert_eq!(
        *asked.lock().unwrap(),
        [0, 1, 2],
        "never asked at turn 0 again"
    );
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 5, "{log:?}");
    assert_eq!(tool_results(&log[2]).len(), 1);
    assert!(
        !tool_results(&log[2])[0].1,
        "a real result, not the feedback"
    );
    assert_eq!(log[3], Message::user_text(reminder("plan first")));
    assert_eq!(log[4], Message::assistant_text("done"));
}
