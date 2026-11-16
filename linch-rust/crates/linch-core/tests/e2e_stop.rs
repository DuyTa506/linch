//! The `stop` and `post_turn` points in a run: feedback that keeps a run
//! going, a closed failure that ends it failed, open failures that leave
//! the run alone, and a crash at every write of a continued run.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{HookOutcome, ResultSubtype, UserEventSubtype};
use linch_core::hooks::{PostTurn, Stop, StopResponse};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, FailMode, HookPoint, Hooks, Message,
    PermissionMode, RunOptions, RunStatus, RunStore, SessionStore, StopReason, ToolScope,
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
    // What the model reads is Python's: the feedback in a system reminder.
    let want = [
        Message::user_text("go"),
        Message::assistant_text("first"),
        Message::user_text("<system-reminder>\ncheck again\n</system-reminder>"),
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

/// A rewrite replaces the run's reported final answer, not the assistant
/// message already appended to the session: that message was logged by
/// `accept` before `stop` ever ran.
#[tokio::test]
async fn a_stop_rewrite_changes_the_final_text_but_not_the_logged_message() {
    let hooks = Hooks::default();
    let polish = hook::<Stop>("polish", |_| {
        Ok(StopResponse::Rewrite {
            text: "Done!".into(),
        })
    });
    let _ = hooks.stop.register(polish.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("first")]));
    let agent = hooked(spec(), provider, hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = [Message::user_text("go"), Message::assistant_text("first")];
    assert_eq!(
        messages(&*stores, "s1").await,
        want,
        "the rewrite does not change the logged assistant message"
    );
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.final_text.as_deref(), Some("Done!"));
}

/// A block ends the run failed, like a closed failure, but by an explicit
/// policy decision rather than an unchecked error.
#[tokio::test]
async fn a_stop_block_ends_the_run_failed() {
    let hooks = Hooks::default();
    let moderate = hook::<Stop>("moderate", |_| {
        Ok(StopResponse::Block {
            reason: "unsafe content".into(),
        })
    });
    let _ = hooks.stop.register(moderate.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![text_turn("first")]));
    let agent = hooked(spec(), provider, hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let error = (envelopes.iter())
        .find_map(|e| match &e.event {
            Event::Error(error) => Some(error.error.clone()),
            _ => None,
        })
        .expect("an error event");
    assert_eq!(error["message"], "unsafe content");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Error);
}

/// R1-rpc 3: a stop hook that fails closed ends the run failed, instead of
/// sending the failure back as feedback and paying for one more answer per
/// turn, without end when no turn limit is set.
#[tokio::test]
async fn a_stop_hook_that_fails_closed_ends_the_run_failed() {
    let hooks = Hooks::default();
    let down = hook::<Stop>("judge", |_| Err("judge down".into())).fail_mode(FailMode::Closed);
    let _ = hooks.stop.register(down.arc()).unwrap();
    let turns = Arc::new(Mutex::new(0));
    let seen = Arc::clone(&turns);
    let watch = hook::<PostTurn>("watch", move |_| {
        *seen.lock().unwrap() += 1;
        Ok(())
    });
    let _ = hooks.post_turn.register(watch.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = answers();
    let agent = hooked(spec(), provider.clone(), hooks, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let want = [Message::user_text("go"), Message::assistant_text("first")];
    assert_eq!(messages(&*stores, "s1").await, want);
    assert_eq!(provider.calls().values().sum::<u32>(), 1, "one paid answer");
    assert_eq!(*turns.lock().unwrap(), 0, "the turn failed: no post_turn");
    let error = envelopes.iter().find_map(|e| match &e.event {
        Event::Error(error) => Some(error.error.clone()),
        _ => None,
    });
    let error = error.expect("an error event");
    assert_eq!(error["name"], "HookError");
    assert_eq!(error["message"], "hook `judge` failed");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Error);
    assert!(!kinds(&envelopes).contains(&"loop_guard".to_owned()));
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

#[tokio::test]
async fn stop_and_post_turn_failures_are_open_by_default() {
    let hooks = Hooks::default();
    let judge = hook::<Stop>("judge", |_| Err("judge down".into()));
    let _ = hooks.stop.register(judge.arc()).unwrap();
    let watch = hook::<PostTurn>("watch", |_| Err("sink down".into()));
    let _ = hooks.post_turn.register(watch.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = hooked(spec(), answers(), hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = [Message::user_text("go"), Message::assistant_text("first")];
    assert_eq!(messages(&*stores, "s1").await, want, "the run ends");
    let failed: Vec<_> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Hook(record) => Some((record.policy_id.as_str(), &record.outcome)),
            _ => None,
        })
        .collect();
    let open = |error: &str| HookOutcome::Failed {
        error: error.into(),
        fail_mode: FailMode::Open,
    };
    let (judge, watch) = (open("judge down"), open("sink down"));
    assert_eq!(failed, [("judge", &judge), ("watch", &watch)]);
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(stores.runs.writes().commit, 3, "one text turn");
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

/// `n` text answers, `answer 1` to `answer n`.
fn texts(n: usize) -> Arc<Replay> {
    let turns = (1..=n).map(|i| text_turn(&format!("answer {i}")));
    Arc::new(Replay::new(turns.collect()))
}

fn stop_verdicts(envelopes: &[Envelope]) -> Vec<String> {
    (envelopes.iter())
        .filter_map(|envelope| match &envelope.event {
            Event::Hook(record) if record.point == HookPoint::Stop => match &record.outcome {
                HookOutcome::Answered { verdict } => Some(verdict.clone()),
                other => Some(format!("{other:?}")),
            },
            _ => None,
        })
        .collect()
}

fn limited(max_turns: u32) -> AgentSpec {
    let mut spec = spec();
    spec.max_turns = Some(max_turns);
    spec
}

/// Python's `before_final_answer` `retry` is honoured once per run
/// (`_MAX_FINAL_ANSWER_REENTRIES`): a hook that always asks again gets one
/// more answer, which then stops the run.
#[tokio::test]
async fn a_final_answer_retry_is_honoured_once_per_run() {
    let hooks = Hooks::default();
    let judge = hook::<Stop>("judge", |_| {
        Ok(StopResponse::Retry {
            feedback: "be precise".into(),
        })
    });
    let _ = hooks.stop.register(judge.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = texts(4);
    let agent = hooked(limited(4), provider.clone(), hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let feedback = Message::user_text("<system-reminder>\nbe precise\n</system-reminder>");
    let want = [
        Message::user_text("go"),
        Message::assistant_text("answer 1"),
        feedback.clone(),
        Message::assistant_text("answer 2"),
    ];
    assert_eq!(messages(&*stores, "s1").await, want);
    assert_eq!(provider.calls().values().sum::<u32>(), 2);
    assert_eq!(stop_verdicts(&envelopes), ["retry", "retry"]);
    let notified = envelopes.iter().any(|e| {
        matches!(&e.event, Event::User(user)
            if user.subtype == UserEventSubtype::Notification && user.message == feedback)
    });
    assert!(notified, "the feedback is a durable notification");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.final_text.as_deref(), Some("answer 2"));
}

/// A spent retry counts as an allow, so the hooks after it decide, as
/// Python's stop hooks do once `before_final_answer` let the answer
/// through. While the retry is honoured they are not asked.
#[tokio::test]
async fn a_spent_retry_lets_the_later_stop_hooks_decide() {
    let hooks = Hooks::default();
    let judge = hook::<Stop>("judge", |_| {
        Ok(StopResponse::Retry {
            feedback: "be precise".into(),
        })
    });
    let asked = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::clone(&asked);
    let polish = hook::<Stop>("polish", move |request| {
        seen.lock().unwrap().push(request.text.clone());
        Ok(StopResponse::Rewrite {
            text: "Done!".into(),
        })
    });
    let _ = hooks.stop.register(judge.arc()).unwrap();
    let _ = hooks.stop.register(polish.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let agent = hooked(limited(4), texts(4), hooks, stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(*asked.lock().unwrap(), ["answer 2"]);
    assert_eq!(stop_verdicts(&envelopes), ["retry", "retry", "rewrite"]);
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(result.final_text.as_deref(), Some("Done!"));
}

/// `continue` is Python's stop `force_continue`: no retry budget bounds it,
/// only `max_turns`.
#[tokio::test]
async fn a_stop_continue_is_bounded_only_by_max_turns() {
    let hooks = Hooks::default();
    let more = hook::<Stop>("more", |_| {
        Ok(StopResponse::Continue {
            feedback: "keep going".into(),
        })
    });
    let _ = hooks.stop.register(more.arc()).unwrap();
    let stores = Arc::new(Stores::default());
    let provider = texts(4);
    let agent = hooked(limited(3), provider.clone(), hooks, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert_eq!(provider.calls().values().sum::<u32>(), 3);
    assert_eq!(stop_verdicts(&envelopes), ["continue"; 3]);
    assert!(kinds(&envelopes).contains(&"loop_guard".to_owned()));
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
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
