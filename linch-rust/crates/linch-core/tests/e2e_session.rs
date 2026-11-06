//! Two runs started at the same time on one session: the run that loses
//! the prompt append backs off, and resume appends its prompt after the
//! other run's turn instead of failing reconcile.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, Message, PermissionMode, Phase, RunError, RunOptions, RunStatus, RunStore,
    ScriptedProvider, SessionId, SessionStore, StoreError,
};

fn agent_on<S: RunStore + SessionStore + 'static>(stores: Arc<S>) -> Agent {
    let turns = [text_turn("answer a"), text_turn("answer b")];
    agent(
        Arc::new(ScriptedProvider::new(turns)),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores,
    )
}

fn texts(messages: &[Message]) -> Vec<String> {
    messages.iter().map(Message::text).collect()
}

/// Start two runs together; the stores yield at every session call, so
/// both read the session before either appends its prompt. One wins the
/// append and completes. The other re-saves `Started` past the winner's
/// prompt and backs off with the conflict; resume then puts its prompt
/// after the winner's answer. `fresh`: neither run finds the session, so
/// both create it and one create conflicts too.
async fn race(prompts: [&str; 2], fresh: bool) {
    let stores = Arc::new(Stores {
        interleave: true,
        ..Stores::default()
    });
    if !fresh {
        let created = SessionStore::create(&*stores, NS, &SessionId::new("s1")).await;
        created.unwrap();
    }
    let agent = agent_on(stores.clone());
    let [a, b] = prompts.map(|prompt| agent.run(prompt, RunOptions::new("s1")));
    let ids = [a.run_id().clone(), b.run_id().clone()];
    let ((_, a_error), (_, b_error)) = tokio::join!(drain(a), drain(b));
    let (winner, loser, error) = match (a_error, b_error) {
        (None, Some(error)) => (0, 1, error),
        (Some(error), None) => (1, 0, error),
        other => panic!("one run must back off: {other:?}"),
    };
    assert!(
        matches!(error, RunError::Store(StoreError::Conflict { .. })),
        "{error:?}"
    );
    let record = stores.runs.load(NS, &ids[winner]).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let record = stores.runs.load(NS, &ids[loser]).await.unwrap();
    assert!(matches!(record.checkpoint.phase, Phase::Started { .. }));
    assert!(
        record.checkpoint.session_len >= 1,
        "past the winner's prompt"
    );
    assert_eq!(record.last_seq, 2, "init and user only");

    finish(agent.resume(ids[loser].clone(), RunOptions::new("s1"))).await;
    let record = stores.runs.load(NS, &ids[loser]).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let log = messages(&*stores, "s1").await;
    let expected = [prompts[winner], "answer a", prompts[loser], "answer b"];
    assert_eq!(texts(&log), expected);
    assert_eq!(stores.sessions.writes().create, 1);
}

#[tokio::test]
async fn the_run_that_loses_the_prompt_append_resumes_after_the_other() {
    race(["from a", "from b"], false).await;
}

#[tokio::test]
async fn identical_prompts_are_appended_once_each() {
    // A schedule fires the same prompt each time.
    race(["tick", "tick"], false).await;
}

#[tokio::test]
async fn two_runs_that_both_create_the_session_take_turns() {
    race(["from a", "from b"], true).await;
}

#[tokio::test]
async fn a_started_run_appends_its_prompt_after_a_session_that_moved_on() {
    let stores = Arc::new(Stores::default());
    SessionStore::create(&*stores, NS, &SessionId::new("s1"))
        .await
        .unwrap();
    // B's writes: run create (1), claim (2), prompt append (3) fails.
    let faulty = Arc::new(Faulty::new(stores.clone(), 3, FaultTiming::Before));
    let b = agent_on(faulty).run("from b", RunOptions::new("s1"));
    let b_id = b.run_id().clone();
    assert!(drain(b).await.1.is_some());
    // A whole run lands on the session meanwhile.
    let agent = agent_on(stores.clone());
    finish(agent.run("from a", RunOptions::new("s1"))).await;

    // The resume writes: claim (1), `Started` at the new length (2), the
    // prompt append (3). Crash right after the append lands.
    let crashing = Arc::new(Faulty::new(stores.clone(), 3, FaultTiming::After));
    let resumed = agent_on(crashing).resume(b_id.clone(), RunOptions::new("s1"));
    assert!(drain(resumed).await.1.is_some());
    let writes = stores.log.lock().unwrap().clone();
    let resume = &writes[writes.len() - 3..];
    assert_eq!(resume, [Write::Setup, Write::Commit(vec![]), Write::Append]);
    // The prompt sits at the re-saved length, so it is not appended twice.
    finish(agent.resume(b_id.clone(), RunOptions::new("s1"))).await;
    let log = messages(&*stores, "s1").await;
    assert_eq!(texts(&log), ["from a", "answer a", "from b", "answer b"]);
    let record = stores.runs.load(NS, &b_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}
