//! End-to-end runs on the scripted provider and memory stores: event order,
//! durable seqs, write counts, usage, and the terminal paths.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::provider::scripted::{ScriptedError, TurnBuilder};
use linch_core::{
    Event, PermissionMode, Persistence, RunOptions, RunStatus, RunStore, ScriptedProvider,
    ScriptedTurn, StopReason, ToolScope,
};
use tokio_util::sync::CancellationToken;

fn result(envelopes: &[linch_core::Envelope]) -> linch_core::events::kinds::ResultEvent {
    match &envelopes.last().expect("the stream is not empty").event {
        Event::Result(result) => result.clone(),
        other => panic!("the stream must end with a result, got {other:?}"),
    }
}

#[tokio::test]
async fn a_text_only_run_delivers_durable_events_in_seq_order() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([text_turn("hello")]));
    let agent = agent(
        provider.clone(),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let stream = agent.run("hi", RunOptions::new("s1"));
    fn send<T: Send + 'static>(_: &T) {}
    send(&stream);
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert_eq!(
        kinds(&envelopes),
        [
            "init",
            "user",
            "context_build",
            "partial_assistant",
            "assistant",
            "usage",
            "result"
        ]
    );
    let result = result(&envelopes);
    assert_eq!(result.subtype, ResultSubtype::Success);
    assert_eq!(result.stop_reason, StopReason::EndTurn);
    assert_eq!(result.final_text.as_deref(), Some("hello"));
    assert_eq!(result.total_usage, usage(10, 5));
    assert!(result.usage_complete);

    // The store holds every durable event, journal-only ones included,
    // with seqs 1..=n; the caller saw exactly the streamed ones, in order.
    let stored = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let seqs: Vec<u64> = stored.iter().map(|e| e.seq).collect();
    assert_eq!(seqs, (1..=stored.len() as u64).collect::<Vec<_>>());
    let streamed: Vec<_> = stored
        .iter()
        .filter(|e| e.event.persistence() == Persistence::Both)
        .cloned()
        .collect();
    let durable: Vec<_> = envelopes
        .iter()
        .filter(|e| e.event.persistence() == Persistence::Both)
        .cloned()
        .collect();
    assert_eq!(durable, streamed);
    assert_eq!(agent.events(&run_id, 0).await.unwrap(), streamed);
    // A delta carries the last durable seq, before the assistant event.
    assert!(envelopes[2].seq < envelopes[3].seq);

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 2);
    assert_eq!(log[1].text(), "hello");
    let request = &provider.requests()[0];
    assert_eq!(request.system[0].text, "You are a test agent.");
    assert_eq!(request.messages.len(), 1);
}

/// A tool turn with `k` started tools costs `3 + 2k` run commits and 2
/// session appends.
async fn write_count(k: usize, scope: ToolScope) {
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(2, k, "Echo")));
    let tools = registry(vec![Echo::new("Echo", scope, &runs)]);
    let agent = agent(
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        stores.clone(),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Success);
    let k = k as u64;
    let (run, session) = (stores.runs.writes(), stores.sessions.writes());
    assert_eq!((run.create, run.claim), (1, 1));
    // Two tool turns, then a text turn (delivery, response, result).
    assert_eq!(run.commit, 2 * (3 + 2 * k) + 3, "k = {k}");
    // The prompt, two appends per tool turn, the final answer.
    assert_eq!((session.create, session.append), (1, 1 + 2 * 2 + 1));
    let runs = runs.lock().unwrap();
    assert_eq!(runs.len() as u64, 2 * k);
    assert!(runs.values().all(|&n| n == 1));
}

#[tokio::test]
async fn one_tool_per_turn_costs_five_run_writes() {
    write_count(1, ToolScope::Read).await;
}

#[tokio::test]
async fn three_parallel_reads_per_turn_cost_nine_run_writes() {
    write_count(3, ToolScope::Read).await;
}

#[tokio::test]
async fn tool_results_reach_the_session_in_call_order() {
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 3, "Echo")));
    let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
    let agent = agent(
        provider.clone(),
        tools,
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let starts = kinds(&envelopes)
        .iter()
        .filter(|k| *k == "tool_call_start")
        .count();
    assert_eq!(starts, 3);
    // Progress is stream-only: it carries the seq of the durable start
    // before it and is never stored.
    let progress: Vec<_> = envelopes
        .iter()
        .filter(|e| kind(&e.event) == "tool_progress")
        .collect();
    assert_eq!(progress.len(), 3);
    let durable_seqs: Vec<u64> = envelopes
        .iter()
        .filter(|e| e.event.persistence() == Persistence::Both)
        .map(|e| e.seq)
        .collect();
    assert!(progress.iter().all(|p| durable_seqs.contains(&p.seq)));
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["v0.0", "v0.1", "v0.2"]);
    // The second request carries the results back to the model.
    assert_eq!(provider.requests()[1].messages.len(), 3);
}

#[tokio::test]
async fn unknown_usage_sends_no_usage_event_and_marks_the_total_incomplete() {
    let mut turn = text_turn("hello");
    let Some(linch_core::ProviderEvent::MessageEnd(end)) = turn.events.last_mut() else {
        panic!("a built turn ends with message_end");
    };
    end.usage = None;
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([turn]));
    let agent = agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        stores,
    );
    let envelopes = finish(agent.run("hi", RunOptions::new("s1"))).await;
    assert!(!kinds(&envelopes).contains(&"usage".to_owned()));
    let result = result(&envelopes);
    assert!(!result.usage_complete);
    assert_eq!(result.total_usage, linch_core::Usage::default());
}

#[tokio::test]
async fn an_abort_before_the_first_call_ends_the_run_aborted() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([text_turn("never")]));
    let agent = agent(
        provider.clone(),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let options = RunOptions::new("s1");
    options.cancel.cancel();
    let stream = agent.run("hi", options);
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert_eq!(kinds(&envelopes), ["init", "user", "result"]);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Aborted);
    assert!(provider.requests().is_empty());
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Aborted);
}

#[tokio::test]
async fn an_abort_inside_a_batch_stops_later_calls_and_keeps_the_session_whole() {
    let runs = Runs::default();
    let cancel = CancellationToken::new();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 2, "Write")));
    let writer = Echo::new("Write", ToolScope::Write, &runs).cancelling(cancel.clone());
    let agent = agent(
        provider.clone(),
        registry(vec![writer]),
        policy(PermissionMode::SkipDangerous),
        stores.clone(),
    );
    let mut options = RunOptions::new("s1");
    options.cancel = cancel;
    let envelopes = finish(agent.run("go", options)).await;
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Aborted);
    assert_eq!(provider.requests().len(), 1);
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 3, "the results follow the tool calls");
    assert_eq!(result_texts(&log[2]), ["v0.0", "Tool call aborted"]);
    let runs = runs.lock().unwrap();
    assert_eq!(
        (runs.get("call_0_0"), runs.get("call_0_1")),
        (Some(&1), None)
    );
}

#[tokio::test]
async fn a_provider_error_fails_the_run_with_an_error_result() {
    let stores = Arc::new(Stores::default());
    let failing = ScriptedTurn::failing(ScriptedError::Status {
        status: 503,
        retry_after_ms: None,
        message: "busy".into(),
    });
    let provider = Arc::new(ScriptedProvider::new([failing]));
    let agent = agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let stream = agent.run("hi", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let expected = ["init", "user", "context_build", "error", "result"];
    assert_eq!(kinds(&envelopes), expected);
    let result = result(&envelopes);
    assert_eq!(
        (result.subtype, result.stop_reason),
        (ResultSubtype::Error, StopReason::Error)
    );
    assert!(!result.usage_complete, "the failed call reported no usage");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

/// A response that names one tool-use id twice is refused whole, as
/// Python's protocol error does: no call of it runs and the run fails.
#[tokio::test]
async fn a_response_that_repeats_a_tool_use_id_fails_the_run_before_any_call() {
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let repeated = TurnBuilder::new(MODEL)
        .tool_call("c1", "Echo", &serde_json::json!({"value": "a"}))
        .tool_call("c1", "Echo", &serde_json::json!({"value": "b"}))
        .build();
    let provider = Arc::new(ScriptedProvider::new([repeated, text_turn("unused")]));
    let agent = agent(
        provider,
        registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]),
        policy(PermissionMode::SkipDangerous),
        stores.clone(),
    );
    let stream = agent.run("hi", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let expected = [
        "init",
        "user",
        "context_build",
        "partial_assistant",
        "usage",
        "error",
        "result",
    ];
    assert_eq!(kinds(&envelopes), expected, "no tool call starts");
    let error = (envelopes.iter())
        .find_map(|e| match &e.event {
            Event::Error(error) => Some(serde_json::Value::Object(error.error.clone())),
            _ => None,
        })
        .unwrap();
    assert!(
        error
            .to_string()
            .contains(r#"duplicate tool-use id \"c1\""#),
        "{error}"
    );
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    assert!(runs.lock().unwrap().is_empty(), "no call ran");
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

#[tokio::test]
async fn max_turns_ends_the_run_with_a_loop_guard_and_an_error() {
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Echo")));
    let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
    let mut spec = spec();
    spec.max_turns = Some(1);
    let agent = agent_with(
        spec,
        provider.clone(),
        tools,
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let tail = &kinds(&envelopes)[envelopes.len() - 3..];
    assert_eq!(tail, ["loop_guard", "error", "result"]);
    assert_eq!(result(&envelopes).subtype, ResultSubtype::Error);
    assert_eq!(provider.requests().len(), 1);
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
}

#[tokio::test]
async fn refused_calls_add_no_run_writes() {
    // Each turn: one call that runs and one to an unknown tool.
    let turn = |t: usize| {
        let value = serde_json::json!({"value": "x"});
        let builder = TurnBuilder::new(MODEL).tool_call(&format!("run_{t}"), "Echo", &value);
        builder
            .tool_call(&format!("bad_{t}"), "Missing", &value)
            .build()
    };
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([turn(0), turn(1), text_turn("done")]));
    let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
    let agent = agent(
        provider,
        tools,
        policy(PermissionMode::Default),
        stores.clone(),
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let ends = kinds(&envelopes)
        .iter()
        .filter(|k| *k == "tool_call_end")
        .count();
    assert_eq!(ends, 4, "refused calls still report their end");
    assert_eq!(stores.runs.writes().commit, 2 * (3 + 2) + 3);
    let log = messages(&*stores, "s1").await;
    assert!(result_texts(&log[2])[1].contains("not registered"));
}
