use std::sync::Arc;

use super::*;
use crate::message::Message;
use crate::run::{Checkpoint, Phase};
use crate::session_log::MessageEntry;
use crate::store::conformance;
use crate::usage::Usage;

#[tokio::test]
async fn memory_run_store_passes_the_conformance_suite() {
    conformance::run_store_suite(&MemoryRunStore::new()).await;
}

#[tokio::test]
async fn memory_run_store_admits_one_live_run_per_session() {
    conformance::run_admission_suite(&MemoryRunStore::new()).await;
}

#[tokio::test]
async fn memory_session_store_passes_the_conformance_suite() {
    conformance::session_store_suite(&MemorySessionStore::new()).await;
}

#[tokio::test]
async fn memory_session_store_passes_the_task_suite() {
    conformance::task_store_suite(&MemorySessionStore::new()).await;
}

fn new_run(run_id: &str) -> NewRun {
    NewRun {
        namespace: "ns".into(),
        run_id: RunId::new(run_id),
        session_id: SessionId::new("s"),
        parent_run_id: None,
        contract: "0".repeat(64).parse().unwrap(),
        checkpoint: Checkpoint {
            turn: 0,
            usage: Usage::default(),
            usage_complete: true,
            cost_usd: None,
            session_len: 0,
            phase: Phase::ProviderPending { attempt: 1 },
        },
        events: Vec::new(),
    }
}

#[tokio::test]
async fn run_writes_count_landed_writes_per_method() {
    let store = MemoryRunStore::new();
    store.create(new_run("r1")).await.unwrap();
    assert!(store.create(new_run("r1")).await.is_err());
    let id = RunId::new("r1");
    let stale = store.claim("ns", &id, "w").await.unwrap();
    let lease = store.claim("ns", &id, "w").await.unwrap();
    store.commit(&lease, Commit::default()).await.unwrap();
    assert!(store.commit(&stale, Commit::default()).await.is_err());
    store.load("ns", &id).await.unwrap();
    store.events("ns", &id, 0).await.unwrap();
    let writes = RunWrites {
        create: 1,
        claim: 2,
        commit: 1,
    };
    assert_eq!(store.writes(), writes);
}

#[tokio::test]
async fn session_writes_count_landed_writes_per_method() {
    let store = MemorySessionStore::new();
    let id = SessionId::new("s");
    store.create("ns", &id).await.unwrap();
    assert!(store.create("ns", &id).await.is_err());
    let entry = LogEntry::Message(MessageEntry {
        message: Arc::new(Message::user_text("hi")),
        visible: true,
        historical: true,
    });
    store
        .append("ns", &id, 0, vec![entry.clone()])
        .await
        .unwrap();
    assert!(store.append("ns", &id, 0, vec![entry]).await.is_err());
    store.load("ns", &id).await.unwrap();
    let writes = SessionWrites {
        create: 1,
        append: 1,
    };
    assert_eq!(store.writes(), writes);
}
