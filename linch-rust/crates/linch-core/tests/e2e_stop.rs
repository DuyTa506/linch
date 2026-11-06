//! The `stop` and `post_turn` points in a run: feedback that keeps a run
//! going, a closed failure bounded by the turn limit, and a crash at every
//! write of a continued run.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{ResultSubtype, UserEventSubtype};
use linch_core::hooks::{PostTurn, Stop, StopResponse};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Event, FailMode, Hooks, Message, PermissionMode, RunOptions,
    RunStatus, RunStore, SessionStore, StopReason, ToolScope,
};

/// Keeps the run going once, after the first answer.
fn stop_hooks() -> Hooks {
    let hooks = Hooks::default();
    let check = hook::<Stop>("check", |request| match request.text.as_str() {
        "first" => Ok(StopResponse::Continue {
            feedback: "check again".into(),
        }),
        _ => Ok(StopResponse::Allow),
    });
    let _ = hooks.stop.register(check.arc()).unwrap();
    hooks
}

fn hooked<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<Replay>,
    hooks: Hooks,
    stores: Arc<S>,
) -> Agent {
    let runs = Runs::default();
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn answers() -> Arc<Replay> {
    Arc::new(Replay::new(vec![text_turn("first"), text_turn("second")]))
}

#[tokio::test]
async fn a_stop_hook_keeps_the_run_going_with_feedback() {
    let stores = Arc::new(Stores::default());
    let provider = answers();
    let agent = hooked(spec(), provider.clone(), stop_hooks(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = [
        Message::user_text("go"),
        Message::assistant_text("first"),
        Message::user_text("check again"),
        Message::assistant_text("second"),
    ];
    assert_eq!(messages(&*stores, "s1").await, want);
    let notified = envelopes.iter().any(|e| {
        matches!(&e.event,
        Event::User(user) if user.subtype == UserEventSubtype::Notification
            && user.message == want[2])
    });
    assert!(notified, "the feedback is a durable notification");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.final_text.as_deref(), Some("second"));
    assert_eq!(result.total_usage, usage(20, 10));
    // Two text turns (pending, response) plus the feedback and the terminal.
    assert_eq!(stores.runs.writes().commit, 6);
    assert_eq!(stores.sessions.writes().append, 4);
}

#[tokio::test]
async fn a_stop_hook_that_fails_closed_runs_until_the_turn_limit() {
    let hooks = Hooks::default();
    let down = hook::<Stop>("judge", |_| Err("judge down".into())).fail_mode(FailMode::Closed);
    let _ = hooks.stop.register(down.arc()).unwrap();
    let mut spec = spec();
    spec.max_turns = Some(2);
    let stores = Arc::new(Stores::default());
    let agent = hooked(spec, answers(), hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(log[2], Message::user_text("hook `judge` failed"));
    assert_eq!(log.len(), 5, "two answers, each sent back");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert!(kinds(&envelopes).contains(&"loop_guard".to_owned()));
}

#[tokio::test]
async fn post_turn_sees_every_finished_turn() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let hooks = stop_hooks();
    let log = Arc::clone(&seen);
    let watch = hook::<PostTurn>("watch", move |request| {
        log.lock()
            .unwrap()
            .push((request.turn, request.stop_reason));
        Ok(())
    });
    let _ = hooks.post_turn.register(watch.arc()).unwrap();
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 1, "Read"),
        text_turn("first"),
        text_turn("second"),
    ]));
    let stores = Arc::new(Stores::default());
    let agent = hooked(spec(), provider, hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let turns = seen.lock().unwrap().clone();
    let want = [
        (0, StopReason::ToolUse),
        (1, StopReason::EndTurn),
        (2, StopReason::EndTurn),
    ];
    assert_eq!(turns, want);
    let records = (envelopes.iter())
        .filter(|e| matches!(&e.event, Event::Hook(r) if r.policy_id == "watch"))
        .count();
    assert_eq!(records, 3);
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_continued_run_recovers_the_session() {
    let clean = Arc::new(Stores::default());
    let agent = hooked(spec(), answers(), stop_hooks(), clean.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let provider = answers();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = hooked(spec(), provider.clone(), stop_hooks(), faulty)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = hooked(spec(), provider.clone(), stop_hooks(), stores.clone());
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
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
}
