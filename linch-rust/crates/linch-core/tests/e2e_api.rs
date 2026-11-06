//! The agent API around runs: caller-chosen run ids and the namespace.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    PermissionMode, RunError, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider, SessionId,
    SessionStore, StoreError,
};

fn options(session: &str, run_id: &str) -> RunOptions {
    let mut options = RunOptions::new(session);
    options.run_id = Some(RunId::new(run_id));
    options
}

#[tokio::test]
async fn a_caller_chosen_run_id_names_the_run() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([text_turn("hello")]));
    let agent = agent(
        provider,
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    assert_eq!(agent.namespace(), NS);
    let stream = agent.run("hi", options("s1", "job-7"));
    assert_eq!(stream.run_id().as_str(), "job-7");
    let envelopes = finish(stream).await;
    assert!(envelopes.iter().all(|e| e.run_id.as_str() == "job-7"));
    let record = stores.runs.load(NS, &RunId::new("job-7")).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
}

#[tokio::test]
async fn a_duplicate_run_id_is_a_conflict_and_writes_nothing() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new([text_turn("one"), text_turn("two")]));
    let agent = agent(
        provider.clone(),
        registry(vec![]),
        policy(PermissionMode::Default),
        stores.clone(),
    );
    finish(agent.run("first", options("s1", "dup"))).await;
    let writes = stores.log.lock().unwrap().len();
    let events = stores.runs.events(NS, &RunId::new("dup"), 0).await.unwrap();

    // The same session, then a session that does not exist yet.
    for session in ["s1", "s2"] {
        let (envelopes, error) = drain(agent.run("again", options(session, "dup"))).await;
        assert!(envelopes.is_empty());
        assert!(
            matches!(error, Some(RunError::Store(StoreError::Conflict { .. }))),
            "{error:?}"
        );
    }
    assert_eq!(stores.log.lock().unwrap().len(), writes);
    let after = stores.runs.events(NS, &RunId::new("dup"), 0).await.unwrap();
    assert_eq!(after, events);
    let missing = SessionStore::load(&*stores, NS, &SessionId::new("s2")).await;
    assert!(matches!(missing, Err(StoreError::NotFound { .. })));
    assert_eq!(provider.requests().len(), 1);
}
