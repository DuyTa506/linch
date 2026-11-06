//! One live run per session: a new run on a session that has a run that is
//! not terminal is refused with `SessionBusy` before it is created, so two
//! runs never interleave in one session log. A run that ended between a
//! new run's session load and its create is read again first.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::*;
use futures::{FutureExt, StreamExt};
use linch_core::hooks::ApprovalResponse;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, Message, PermissionMode, RunError, RunId, RunOptions, RunStatus, RunStore,
    ScriptedProvider, ScriptedTurn, SessionId, SessionStore, StoreError, ToolScope,
};

fn agent_on(stores: &Arc<Stores>, turns: Vec<ScriptedTurn>, runs: &Runs) -> Agent {
    agent(
        Arc::new(ScriptedProvider::new(turns)),
        registry(vec![Echo::new("Echo", ToolScope::Write, runs)]),
        policy(PermissionMode::Default),
        stores.clone(),
    )
}

fn texts(messages: &[Message]) -> Vec<String> {
    messages.iter().map(Message::text).collect()
}

/// The `SessionBusy` error of `error`, as (session, run).
fn busy(error: Option<RunError>) -> (SessionId, RunId) {
    match error {
        Some(RunError::Store(StoreError::SessionBusy { session_id, run_id })) => {
            (session_id, run_id)
        }
        other => panic!("expected SessionBusy, got {other:?}"),
    }
}

#[tokio::test]
async fn a_run_on_a_busy_session_is_refused_before_it_is_created() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let turns = vec![
        tool_turn(0, 1, "Echo"),
        text_turn("done"),
        text_turn("answer b"),
    ];
    let agent = agent_on(&stores, turns, &runs);
    // A suspends for approval: still a live run of s1.
    let first = agent.run("first", RunOptions::new("s1"));
    let first_id = first.run_id().clone();
    finish(first).await;
    let writes = stores.log.lock().unwrap().len();

    let second = agent.run("second", RunOptions::new("s1"));
    let second_id = second.run_id().clone();
    let (envelopes, error) = drain(second).await;
    assert!(envelopes.is_empty());
    assert_eq!(busy(error), (SessionId::new("s1"), first_id.clone()));
    assert_eq!(stores.log.lock().unwrap().len(), writes, "nothing written");
    let missing = stores.runs.load(NS, &second_id).await;
    assert!(matches!(missing, Err(StoreError::NotFound { .. })));

    // Once A ends, the session takes a new run.
    let allow = HashMap::from([("call_0_0".to_owned(), ApprovalResponse::Allow)]);
    finish(agent.approve(first_id, allow, RunOptions::new("s1"))).await;
    finish(agent.run("second", RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(log.len(), 6);
    assert_eq!(texts(&log[4..]), ["second", "answer b"]);
}

/// Start two runs together; the stores yield at every session call, so
/// both read the session before either is created. The store admits one;
/// the other is refused, naming the winner, and leaves no trace. `fresh`:
/// neither run finds the session, so both create it too.
async fn race(prompts: [&str; 2], fresh: bool) {
    let stores = Arc::new(Stores {
        interleave: true,
        ..Stores::default()
    });
    if !fresh {
        let created = SessionStore::create(&*stores, NS, &SessionId::new("s1")).await;
        created.unwrap();
    }
    let turns = vec![text_turn("answer a"), text_turn("answer b")];
    let agent = agent_on(&stores, turns, &Runs::default());
    let [a, b] = prompts.map(|prompt| agent.run(prompt, RunOptions::new("s1")));
    let ids = [a.run_id().clone(), b.run_id().clone()];
    let ((_, a_error), (_, b_error)) = tokio::join!(drain(a), drain(b));
    let (winner, loser, error) = match (a_error, b_error) {
        (None, error @ Some(_)) => (0, 1, error),
        (error @ Some(_), None) => (1, 0, error),
        other => panic!("exactly one run is admitted: {other:?}"),
    };
    assert_eq!(busy(error), (SessionId::new("s1"), ids[winner].clone()));
    let record = stores.runs.load(NS, &ids[winner]).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let missing = stores.runs.load(NS, &ids[loser]).await;
    assert!(matches!(missing, Err(StoreError::NotFound { .. })));
    let log = messages(&*stores, "s1").await;
    assert_eq!(texts(&log), [prompts[winner], "answer a"]);

    // Started again once the winner is done, it goes after it.
    finish(agent.run(prompts[loser], RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    let expected = [prompts[winner], "answer a", prompts[loser], "answer b"];
    assert_eq!(texts(&log), expected);
    assert_eq!(stores.sessions.writes().create, 1);
}

#[tokio::test]
async fn two_runs_started_together_on_one_session_admit_one() {
    race(["from a", "from b"], false).await;
}

#[tokio::test]
async fn identical_prompts_never_share_a_run() {
    // A schedule fires the same prompt each time.
    race(["tick", "tick"], false).await;
}

#[tokio::test]
async fn two_runs_that_both_create_the_session_admit_one() {
    race(["from a", "from b"], true).await;
}

/// Stores that yield at every session call, with an empty session `s1`.
async fn interleaved() -> Arc<Stores> {
    let stores = Arc::new(Stores {
        interleave: true,
        ..Stores::default()
    });
    let created = SessionStore::create(&*stores, NS, &SessionId::new("s1")).await;
    created.unwrap();
    stores
}

/// B reads the empty session, then A runs to the end, then B goes on: it
/// is admitted (A is terminal), its first append finds A's entries, and it
/// reads the session again and appends after them, even with A's prompt.
async fn read_again(prompts: [&str; 2]) {
    let stores = interleaved().await;
    let turns = vec![text_turn("answer a"), text_turn("answer b")];
    let agent = agent_on(&stores, turns, &Runs::default());
    let mut b = agent.run(prompts[1], RunOptions::new("s1"));
    assert!(b.next().now_or_never().is_none(), "B parks after its load");
    finish(agent.run(prompts[0], RunOptions::new("s1"))).await;
    let commits = stores.runs.writes().commit;
    finish(b).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        texts(&log),
        [prompts[0], "answer a", prompts[1], "answer b"]
    );
    // `Started` saved again at the new length, then a text-only turn.
    assert_eq!(stores.runs.writes().commit - commits, 1 + 3);
}

#[tokio::test]
async fn a_run_that_ended_after_the_load_is_read_again() {
    read_again(["from a", "from b"]).await;
}

#[tokio::test]
async fn an_identical_prompt_of_an_ended_run_is_not_adopted() {
    read_again(["tick", "tick"]).await;
}

#[tokio::test]
async fn resume_appends_the_prompt_after_runs_that_ended_before_it() {
    let stores = interleaved().await;
    let turns = || vec![text_turn("answer a"), text_turn("answer b")];
    // B's writes: run create (1), claim (2), prompt append (3) fails.
    let faulty = Arc::new(Faulty::new(stores.clone(), 3, FaultTiming::Before));
    let runs = Runs::default();
    let crashing = agent(
        Arc::new(ScriptedProvider::new(turns())),
        registry(vec![Echo::new("Echo", ToolScope::Write, &runs)]),
        policy(PermissionMode::Default),
        faulty,
    );
    let mut b = crashing.run("from b", RunOptions::new("s1"));
    let b_id = b.run_id().clone();
    assert!(b.next().now_or_never().is_none(), "B parks after its load");
    let agent = agent_on(&stores, turns(), &runs);
    finish(agent.run("from a", RunOptions::new("s1"))).await;
    assert!(drain(b).await.1.is_some(), "B's first append fails");

    let before = stores.log.lock().unwrap().len();
    finish(agent.resume(b_id.clone(), RunOptions::new("s1"))).await;
    // Claim, `Started` at the new length (with the resume's `init`), then
    // the prompt append.
    let writes = stores.log.lock().unwrap()[before..before + 3].to_vec();
    let started = Write::Commit(vec!["init".into()]);
    assert_eq!(writes, [Write::Setup, started, Write::Append]);
    let log = messages(&*stores, "s1").await;
    assert_eq!(texts(&log), ["from a", "answer a", "from b", "answer b"]);
    let record = stores.runs.load(NS, &b_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}
