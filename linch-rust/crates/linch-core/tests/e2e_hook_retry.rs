//! Hooks that ask for another turn: an `after_provider_call` retry, which
//! accepts the paid response but sends feedback back instead of acting on
//! it, and a crash at every write of a retried turn.

mod common;

use std::sync::{Arc, Mutex};

use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{HookOutcome, UserEventSubtype};
use linch_core::events::model_call::CallDisposition;
use linch_core::hooks::{AfterProviderCall, AfterProviderCallResponse, Stop, StopResponse};
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

fn accepted_receipts(envelopes: &[Envelope]) -> usize {
    (envelopes.iter())
        .filter(|e| {
            matches!(&e.event, Event::ModelCallReceipt(receipt)
                if receipt.disposition == CallDisposition::Accepted)
        })
        .count()
}

/// The retry decision is durable with the paid response: a crash at any
/// write replays it exactly once, never runs the rejected tool call, and
/// never pays again for a response that was already received.
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
            assert_eq!(accepted_receipts(&events), 2, "{context}: one per turn");
            let calls = provider.calls();
            assert!(calls.values().all(|&n| n <= 2), "{context}: {calls:?}");
        }
    }
}
