//! Crash a run at every store write, or drop its stream after every
//! envelope, then continue it, and compare with a run that never stopped.
//!
//! A tool whose start is durable and whose end is not is never run again:
//! its result becomes the interrupted placeholder (13.1.4). So the final
//! session equals the uncrashed one, except that a crash on a tool barrier
//! write (or a drop right after a tool start) may leave placeholders.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::*;
use linch_core::events::kinds::ResultSubtype;
use linch_core::events::model_call::CallDisposition;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, ContentBlock, Envelope, Event, Message, PermissionMode, RunId, RunOptions, RunStore,
    SessionStore, StoreError, ToolResultBlock, ToolScope,
};

/// Two parallel reads, one write, then the answer.
fn turns() -> Vec<linch_core::ScriptedTurn> {
    vec![
        tool_turn(0, 2, "Read"),
        tool_turn(1, 1, "Write"),
        text_turn("done"),
    ]
}

fn tools(runs: &Runs) -> Arc<linch_core::ToolRegistry> {
    registry(vec![
        Echo::new("Read", ToolScope::Read, runs),
        Echo::new("Write", ToolScope::Write, runs),
    ])
}

/// The clean run: its final session and the kind of every write it made.
async fn baseline() -> (Vec<Message>, Vec<Write>) {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(turns()));
    let runs = Runs::default();
    let agent = agent(
        provider,
        tools(&runs),
        policy(PermissionMode::SkipDangerous),
        stores.clone(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    let writes = stores.log.lock().unwrap().clone();
    (log, writes)
}

/// Compare `got` with `want`; tool results may be the interrupted
/// placeholder. Returns the call id of every result, and whether it is one.
fn compare(got: &[Message], want: &[Message], context: &str) -> Vec<(String, bool)> {
    let placeholder = ToolResultBlock::text("", INTERRUPTED_TOOL_TEXT, true).content;
    assert_eq!(got.len(), want.len(), "{context}");
    let mut results = Vec::new();
    for (got, want) in got.iter().zip(want) {
        assert_eq!(got.content.len(), want.content.len(), "{context}");
        for (got, want) in got.content.iter().zip(&want.content) {
            if let (ContentBlock::ToolResult(block), ContentBlock::ToolResult(expected)) =
                (got, want)
            {
                let interrupted = block.is_error && block.content == placeholder;
                assert_eq!(block.tool_use_id, expected.tool_use_id, "{context}");
                results.push((block.tool_use_id.clone(), interrupted));
                if interrupted {
                    continue;
                }
            }
            assert_eq!(got, want, "{context}");
        }
    }
    results
}

/// Turn index of a model-call event, from its logical id `{run}:primary:{turn}`.
fn turn_of(logical_call_id: &str) -> usize {
    logical_call_id.rsplit(':').next().unwrap().parse().unwrap()
}

/// No paid response is requested twice: each turn has one accepted receipt,
/// and every extra provider call was settled by an abandoned receipt.
fn check_provider_calls(calls: &HashMap<usize, u32>, events: &[Envelope], context: &str) {
    let mut accepted: HashMap<usize, u32> = HashMap::new();
    let mut abandoned: HashMap<usize, u32> = HashMap::new();
    for envelope in events {
        if let Event::ModelCallReceipt(receipt) = &envelope.event {
            let turn = turn_of(&receipt.logical_call_id);
            match receipt.disposition {
                CallDisposition::Accepted => *accepted.entry(turn).or_default() += 1,
                CallDisposition::Cancelled => *abandoned.entry(turn).or_default() += 1,
                other => panic!("{context}: unexpected receipt {other:?}"),
            }
        }
    }
    for turn in 0..turns().len() {
        assert_eq!(accepted.get(&turn), Some(&1), "{context}: turn {turn}");
        let calls = calls.get(&turn).copied().unwrap_or(0);
        let spare = abandoned.get(&turn).copied().unwrap_or(0);
        assert!(
            calls >= 1 && calls <= 1 + spare,
            "{context}: turn {turn} called {calls} times"
        );
    }
}

/// Continue whatever the stores hold after a stop: resume the run, or start
/// it again if it was never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: &RunId) -> RunId {
    match stores.runs.load(NS, run_id).await {
        // The fault hit the terminal write after it landed.
        Ok(record) if record.status.is_terminal() => run_id.clone(),
        Ok(_) => {
            let envelopes = finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            assert!(matches!(&envelopes.last().unwrap().event,
                Event::Result(r) if r.subtype == ResultSubtype::Success));
            run_id.clone()
        }
        Err(StoreError::NotFound { .. }) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
        Err(error) => panic!("load failed: {error}"),
    }
}

/// After a stop and a recovery: compare the session, count tool runs and
/// provider calls, and check the durable seqs.
async fn check(
    stores: &Stores,
    run_id: &RunId,
    provider: &Replay,
    runs: &Runs,
    want: &[Message],
    placeholders_ok: bool,
    context: &str,
) {
    let got = messages(stores, "s1").await;
    let results = compare(&got, want, context);
    // The frozen prefix rides the first prompt's write: logged once, first.
    let entries = SessionStore::load(stores, NS, &linch_core::SessionId::new("s1"))
        .await
        .unwrap();
    let prefixes: Vec<usize> = (entries.iter().enumerate())
        .filter(|(_, entry)| matches!(entry, linch_core::LogEntry::Prefix(_)))
        .map(|(at, _)| at)
        .collect();
    assert_eq!(prefixes, [0], "{context}: prefixes at {prefixes:?}");
    let placeholders = results
        .iter()
        .filter(|(_, interrupted)| *interrupted)
        .count();
    assert!(
        placeholders == 0 || placeholders_ok,
        "{context}: {placeholders} placeholders"
    );
    let runs = runs.lock().unwrap().clone();
    assert!(
        runs.values().all(|&n| n == 1),
        "{context}: a tool ran twice: {runs:?}"
    );
    for (id, interrupted) in &results {
        // A real result means the tool ran once; a placeholder, at most once.
        assert!(
            *interrupted || runs.get(id) == Some(&1),
            "{context}: {id} never ran"
        );
    }
    let events = stores.runs.events(NS, run_id, 0).await.unwrap();
    // Every tool that started ends, an interrupted one with its placeholder.
    for envelope in &events {
        if let Event::ToolCallStart(start) = &envelope.event {
            let id = start.tool_use_id.as_str();
            let ends = (events.iter())
                .filter(|e| matches!(&e.event, Event::ToolCallEnd(end) if end.tool_use_id() == id))
                .count();
            assert_eq!(ends, 1, "{context}: {id} has {ends} ends");
        }
    }
    let seqs: Vec<u64> = events.iter().map(|e| e.seq).collect();
    assert_eq!(
        seqs,
        (1..=events.len() as u64).collect::<Vec<_>>(),
        "{context}"
    );
    check_provider_calls(&provider.calls(), &events, context);
}

#[tokio::test]
async fn a_crash_at_any_write_recovers_to_the_same_session() {
    let (want, writes) = baseline().await;
    assert!(
        writes.len() > 20,
        "the scenario has many barriers: {writes:?}"
    );
    for (index, write) in writes.iter().enumerate() {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let n = index as u64 + 1;
            let context = format!("write {n} ({write:?}) failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let provider = Arc::new(Replay::new(turns()));
            let runs = Runs::default();
            let permissions = || policy(PermissionMode::SkipDangerous);
            let crashing = agent(provider.clone(), tools(&runs), permissions(), faulty);
            let stream = crashing.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            assert!(error.is_some(), "{context}: the run must stop");
            let clean = agent(
                provider.clone(),
                tools(&runs),
                permissions(),
                stores.clone(),
            );
            let run_id = recover(&clean, &stores, &run_id).await;
            let barrier = write.is_tool_barrier();
            check(&stores, &run_id, &provider, &runs, &want, barrier, &context).await;
        }
    }
}

#[tokio::test]
async fn a_stream_dropped_after_any_envelope_resumes_to_the_same_session() {
    let (want, _) = baseline().await;
    let total = {
        let stores = Arc::new(Stores::default());
        let agent = agent(
            Arc::new(Replay::new(turns())),
            tools(&Runs::default()),
            policy(PermissionMode::SkipDangerous),
            stores,
        );
        finish(agent.run("go", RunOptions::new("s1"))).await.len()
    };
    for keep in 1..total {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Replay::new(turns()));
        let runs = Runs::default();
        let agent = agent(
            provider.clone(),
            tools(&runs),
            policy(PermissionMode::SkipDangerous),
            stores.clone(),
        );
        let mut stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        let mut last = String::new();
        for _ in 0..keep {
            let envelope = futures::StreamExt::next(&mut stream)
                .await
                .unwrap()
                .unwrap();
            last = kind(&envelope.event);
        }
        drop(stream);
        let context = format!("dropped after {keep} envelopes (last {last})");
        recover(&agent, &stores, &run_id).await;
        let at_start = last == "tool_call_start";
        check(
            &stores, &run_id, &provider, &runs, &want, at_start, &context,
        )
        .await;
    }
}

/// A batch resumed after a crash keeps its first `after_seq`, so a second
/// crash inside it still sees the calls that finished before the first.
#[tokio::test]
async fn a_second_crash_in_a_resumed_batch_does_not_rerun_finished_calls() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(vec![
        tool_turn(0, 2, "Write"),
        text_turn("done"),
    ]));
    let runs = Runs::default();
    fn make<S: RunStore + SessionStore + 'static>(
        p: &Arc<Replay>,
        runs: &Runs,
        stores: Arc<S>,
    ) -> Agent {
        agent(
            p.clone(),
            tools(runs),
            policy(PermissionMode::SkipDangerous),
            stores,
        )
    }
    // Writes: session create, run create, claim, prompt, provider pending,
    // response, append, then call_0_0 start (8) and end (9), call_0_1 start
    // (10). Fail before 10: call_0_0 is done, call_0_1 never started.
    let first = Arc::new(Faulty::new(stores.clone(), 10, FaultTiming::Before));
    let stream = make(&provider, &runs, first).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    assert!(drain(stream).await.1.is_some());
    // The resume writes: claim (1), call_0_1 start with the batch checkpoint
    // (2), its end (3). Fail before 3: call_0_1 ran, its end is lost.
    let second = Arc::new(Faulty::new(stores.clone(), 3, FaultTiming::Before));
    let resumed = make(&provider, &runs, second).resume(run_id.clone(), RunOptions::new("s1"));
    let (_, error) = drain(resumed).await;
    assert!(error.is_some());
    finish(make(&provider, &runs, stores.clone()).resume(run_id, RunOptions::new("s1"))).await;
    let runs = runs.lock().unwrap().clone();
    assert_eq!((runs["call_0_0"], runs["call_0_1"]), (1, 1));
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["v0.0", INTERRUPTED_TOOL_TEXT]);
}
