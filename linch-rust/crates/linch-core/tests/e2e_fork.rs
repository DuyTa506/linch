//! Forking a session: the destination gets a copy of a durable prefix of
//! the source's log, ending outside a tool exchange, and a run on it goes
//! on from that history while the source stays as it was.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    ForkError, LogEntry, PermissionMode, RunOptions, ScriptedProvider, SessionId, SessionStore,
    StoreError, ToolScope,
};

fn s(id: &str) -> SessionId {
    SessionId::new(id)
}

/// `s1` after one run: prompt, a call, its results, the answer.
async fn source() -> (Arc<Stores>, linch_core::Agent, Arc<ScriptedProvider>) {
    let runs = Runs::default();
    let mut turns = script(1, 1, "Echo");
    turns.push(text_turn("forked answer"));
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
    let agent = agent(
        provider.clone(),
        tools,
        policy(PermissionMode::SkipDangerous),
        stores.clone(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    (stores, agent, provider)
}

#[tokio::test]
async fn a_fork_copies_the_log_and_a_run_on_it_goes_on_from_there() {
    let (stores, agent, provider) = source().await;
    let before = stores.sessions.load(NS, &s("s1")).await.unwrap();
    assert_eq!(before.len(), 4);

    assert_eq!(
        agent.fork_session(&s("s1"), &s("f1"), None).await.unwrap(),
        4
    );
    let copied = stores.sessions.load(NS, &s("f1")).await.unwrap();
    assert_eq!(copied, before);

    finish(agent.run("and then?", RunOptions::new("f1"))).await;
    let request = provider.requests().pop().unwrap();
    let texts: Vec<String> = request.messages.iter().map(|m| m.text()).collect();
    assert_eq!(texts[0], "go");
    assert_eq!(texts.last().unwrap(), "and then?");
    assert_eq!(request.messages.len(), 5);
    let after = stores.sessions.load(NS, &s("s1")).await.unwrap();
    assert_eq!(after, before, "the source is untouched");
}

#[tokio::test]
async fn a_fork_before_an_entry_copies_only_what_precedes_it() {
    let (stores, agent, _) = source().await;
    assert_eq!(
        agent
            .fork_session(&s("s1"), &s("f1"), Some(1))
            .await
            .unwrap(),
        1
    );
    let copied = stores.sessions.load(NS, &s("f1")).await.unwrap();
    let [LogEntry::Message(prompt)] = copied.as_slice() else {
        panic!("one entry: {copied:?}")
    };
    assert_eq!(prompt.message.text(), "go");
    assert_eq!(
        agent
            .fork_session(&s("s1"), &s("f0"), Some(0))
            .await
            .unwrap(),
        0
    );
    assert!(stores.sessions.load(NS, &s("f0")).await.unwrap().is_empty());
}

#[tokio::test]
async fn a_fork_inside_a_tool_exchange_is_refused_and_writes_nothing() {
    let (stores, agent, _) = source().await;
    let error = agent.fork_session(&s("s1"), &s("f1"), Some(2)).await;
    assert!(
        matches!(&error, Err(ForkError::Unpaired(ids)) if ids == "call_0_0"),
        "{error:?}"
    );
    let missing = stores.sessions.load(NS, &s("f1")).await;
    assert!(matches!(missing, Err(StoreError::NotFound { .. })));
}

#[tokio::test]
async fn a_fork_with_a_bad_boundary_or_destination_is_refused() {
    let (_stores, agent, _) = source().await;
    let fork = |before, dest: &'static str| {
        let agent = agent.clone();
        async move { agent.fork_session(&s("s1"), &s(dest), before).await }
    };
    assert!(matches!(
        fork(Some(4), "f1").await,
        Err(ForkError::Boundary { before: 4, len: 4 })
    ));
    assert!(matches!(
        fork(None, "s1").await,
        Err(ForkError::SameSession)
    ));
    fork(None, "f1").await.unwrap();
    assert!(matches!(
        fork(None, "f1").await,
        Err(ForkError::Exists(dest)) if dest == s("f1")
    ));
    let missing = agent.fork_session(&s("nope"), &s("f2"), None).await;
    assert!(matches!(
        missing,
        Err(ForkError::Store(StoreError::NotFound { .. }))
    ));
}
