//! A writer another owner fenced out reports a lost lease, whichever write
//! finds out: the session append that loses to the new owner's entries is
//! `LeaseLost`, not a conflict, which only a duplicate run id is.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, JournalError, LogEntry, MessageEntry, PermissionEngine,
    PermissionMode, PermissionPolicy, RunError, RunId, RunOptions, RunStore, ScriptedProvider,
    SessionId, SessionStore, StoreError,
};
use linch_postgres::{PgRunStore, PgSessionStore};

use crate::common::TestDb;

const MODEL: &str = "scripted-model";
const NS: &str = "ns";

/// A session store that, just before the run's `at`th append (from 1), lets
/// another owner take the run over and write to the session: the race a
/// fenced writer loses when no journal write came in between to tell it.
struct Takeover {
    sessions: PgSessionStore,
    other: PgRunStore,
    run_id: RunId,
    at: u64,
    appends: AtomicU64,
}

#[async_trait]
impl SessionStore for Takeover {
    async fn create(&self, namespace: &str, session_id: &SessionId) -> Result<(), StoreError> {
        self.sessions.create(namespace, session_id).await
    }

    async fn load(
        &self,
        namespace: &str,
        session_id: &SessionId,
    ) -> Result<Vec<LogEntry>, StoreError> {
        self.sessions.load(namespace, session_id).await
    }

    async fn append(
        &self,
        namespace: &str,
        session_id: &SessionId,
        expected_len: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        if self.appends.fetch_add(1, Ordering::SeqCst) + 1 == self.at {
            let seized = self.other.seize(namespace, &self.run_id, "other").await;
            seized.expect("the other owner takes the run");
            let message = linch_core::Message::user_text("written by the new owner");
            let entry = LogEntry::Message(MessageEntry {
                message: Arc::new(message),
                visible: true,
                historical: true,
            });
            let appended = (self.sessions)
                .append(namespace, session_id, expected_len, vec![entry])
                .await;
            appended.expect("the new owner appends");
        }
        (self.sessions)
            .append(namespace, session_id, expected_len, entries)
            .await
    }
}

fn agent(db: &TestDb, run_id: &RunId, at: u64) -> Agent {
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let provider = ScriptedProvider::new([TurnBuilder::new(MODEL).text("done").build()]);
    let sessions = Takeover {
        sessions: PgSessionStore::new(db.pool.clone()),
        other: PgRunStore::new(db.pool.clone()),
        run_id: run_id.clone(),
        at,
        appends: AtomicU64::new(0),
    };
    AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::new(provider))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(PgRunStore::new(db.pool.clone())))
        .session_store(Arc::new(sessions))
        .namespace(NS)
        .build()
        .unwrap()
}

async fn last_error(agent: &Agent, run_id: &RunId) -> RunError {
    let options = RunOptions {
        run_id: Some(run_id.clone()),
        ..RunOptions::new("s1")
    };
    let items: Vec<_> = agent.run("hi", options).collect().await;
    match items.into_iter().last() {
        Some(Err(error)) => error,
        other => panic!("the run ends with an error: {other:?}"),
    }
}

/// The new owner's entries moved the session while this run still held a
/// lease it no longer had: the error is the lost lease a journal write
/// reports, naming the run and the epoch that was fenced.
#[tokio::test]
async fn a_session_append_that_loses_to_a_new_owner_is_a_lost_lease() {
    let Some(db) = TestDb::migrated("fence_append").await else {
        return;
    };
    let run_id = RunId::new("fenced");
    // Append 1 is the prompt; append 2 is the model's answer.
    let agent = agent(&db, &run_id, 2);
    let error = last_error(&agent, &run_id).await;
    assert!(
        matches!(
            &error,
            RunError::Journal(JournalError::Store(StoreError::LeaseLost { run_id: lost, epoch }))
                if lost == &run_id && *epoch == 1
        ),
        "{error:?}"
    );
    db.finish().await;
}

/// A chosen run id that is already stored stays a conflict.
#[tokio::test]
async fn a_duplicate_run_id_is_still_a_conflict() {
    let Some(db) = TestDb::migrated("fence_duplicate").await else {
        return;
    };
    let run_id = RunId::new("twice");
    let agent = agent(&db, &run_id, 0);
    let first: Vec<_> = (agent.run(
        "hi",
        RunOptions {
            run_id: Some(run_id.clone()),
            ..RunOptions::new("s1")
        },
    ))
    .collect()
    .await;
    assert!(first.iter().all(Result::is_ok), "{first:?}");
    let error = last_error(&agent, &run_id).await;
    assert!(
        matches!(&error, RunError::Store(StoreError::Conflict { .. })),
        "{error:?}"
    );
    db.finish().await;
}
