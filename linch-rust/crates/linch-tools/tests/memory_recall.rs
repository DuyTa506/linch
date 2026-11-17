//! `MemoryRecall`: what it searches with, what it adds, and what it leaves
//! out.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{ContextBuild, ContextBuildRequest};
use linch_core::{
    ContentBlock, Hook, InMemoryMemoryStore, Memory, MemoryError, MemoryStore, Message, NewMemory,
    Principal, Role, RunId, SessionId, TextBlock, ToolResultBlock,
};
use linch_tools::{MemoryRecall, MemoryScope};

fn request(messages: Vec<Message>, principal: Option<Principal>) -> ContextBuildRequest {
    ContextBuildRequest {
        session_id: SessionId::from("s"),
        run_id: RunId::from("r"),
        messages: Arc::new(messages.into_iter().map(Arc::new).collect()),
        principal,
    }
}

async fn store_with(scope: Option<&str>, facts: &[&str]) -> Arc<InMemoryMemoryStore> {
    let store = Arc::new(InMemoryMemoryStore::new());
    for (n, fact) in facts.iter().enumerate() {
        let memory = NewMemory::new(format!("k{n}"), fact, &[]).unwrap();
        store.save(scope, memory).await.unwrap();
    }
    store
}

fn ann() -> Principal {
    Principal::new("acme", "ann", ["user"]).unwrap()
}

#[tokio::test]
async fn the_latest_user_text_is_the_query() {
    let store = store_with(None, &["Ann likes green tea", "The deploy runs on Fridays"]).await;
    let recall = MemoryRecall::new(store, MemoryScope::Agent, 5);
    let messages = vec![
        Message::user_text("what tea do I like?"),
        Message::assistant_text("let me check"),
        // A tool result and runtime feedback are not what the user wrote.
        Message {
            role: Role::User,
            content: vec![
                ContentBlock::ToolResult(ToolResultBlock::text(
                    "t1",
                    "deploy deploy deploy",
                    false,
                )),
                ContentBlock::Text(TextBlock {
                    text: "<system-reminder>deploy deploy</system-reminder>".into(),
                }),
            ],
            provider_metadata: None,
        },
    ];
    let response = recall.call(&request(messages, None)).await.unwrap();
    assert_eq!(response.blocks.len(), 1);
    let text = &response.blocks[0].text;
    assert!(text.contains("Ann likes green tea"), "{text}");
    assert!(!text.contains("deploy"), "{text}");
    assert_eq!(
        response.metadata["memory"]["ids"].as_array().unwrap().len(),
        1
    );
}

#[tokio::test]
async fn it_adds_at_most_k_memories_and_nothing_for_no_match() {
    let facts: Vec<String> = (0..6).map(|n| format!("tea fact {n}")).collect();
    let refs: Vec<&str> = facts.iter().map(String::as_str).collect();
    let store = store_with(None, &refs).await;
    let recall = MemoryRecall::new(store, MemoryScope::Agent, 3);
    let asked = request(vec![Message::user_text("tea")], None);
    let response = recall.call(&asked).await.unwrap();
    assert_eq!(response.blocks[0].text.matches("[memory:").count(), 3);
    let other = request(vec![Message::user_text("nothing alike")], None);
    assert!(recall.call(&other).await.unwrap().blocks.is_empty());
    let blank = request(vec![Message::user_text("?! ...")], None);
    assert!(recall.call(&blank).await.unwrap().blocks.is_empty());
    assert!(
        recall
            .call(&request(vec![], None))
            .await
            .unwrap()
            .blocks
            .is_empty()
    );
}

#[tokio::test]
async fn a_principal_scope_reads_only_its_principal() {
    let store = Arc::new(InMemoryMemoryStore::new());
    let shared = NewMemory::new("a", "shared tea note", &[]).unwrap();
    store.save(None, shared).await.unwrap();
    let key = linch_core::principal_scope(&ann());
    let own = NewMemory::new("b", "ann tea note", &[]).unwrap();
    store.save(Some(&key), own).await.unwrap();
    let recall = MemoryRecall::new(store, MemoryScope::Principal, 5);

    let asked = request(vec![Message::user_text("tea")], Some(ann()));
    let text = recall.call(&asked).await.unwrap().blocks[0].text.clone();
    assert!(
        text.contains("ann tea note") && !text.contains("shared"),
        "{text}"
    );

    let bob = Principal::new("acme", "bob", ["user"]).unwrap();
    let asked = request(vec![Message::user_text("tea")], Some(bob));
    assert!(recall.call(&asked).await.unwrap().blocks.is_empty());

    // Fails closed: no principal never falls back to the shared memory.
    let asked = request(vec![Message::user_text("tea")], None);
    assert!(recall.call(&asked).await.unwrap().blocks.is_empty());
}

#[test]
fn the_policy_names_the_scope_and_k() {
    let store: Arc<dyn MemoryStore> = Arc::new(InMemoryMemoryStore::new());
    let id = |scope, k| {
        let recall = MemoryRecall::new(store.clone(), scope, k);
        Hook::<ContextBuild>::policy_id(&recall).to_owned()
    };
    assert_ne!(id(MemoryScope::Agent, 5), id(MemoryScope::Principal, 5));
    assert_ne!(id(MemoryScope::Agent, 5), id(MemoryScope::Agent, 6));
    assert_eq!(id(MemoryScope::Agent, 5), id(MemoryScope::Agent, 5));
}

struct Leaky;

#[async_trait]
impl MemoryStore for Leaky {
    async fn save(&self, _: Option<&str>, _: NewMemory) -> Result<Memory, MemoryError> {
        unreachable!()
    }

    async fn search(&self, _: Option<&str>, _: &str, _: usize) -> Result<Vec<Memory>, MemoryError> {
        Err(MemoryError::Backend(
            "down: postgres://u:hunter2@db.internal/x".into(),
        ))
    }

    async fn delete(&self, _: Option<&str>, _: &str) -> Result<bool, MemoryError> {
        unreachable!()
    }
}

#[tokio::test]
async fn a_store_failure_is_an_error_without_the_address() {
    let recall = MemoryRecall::new(Arc::new(Leaky), MemoryScope::Agent, 5);
    let error = recall
        .call(&request(vec![Message::user_text("tea")], None))
        .await
        .unwrap_err();
    let shown = format!("{error} {error:?}");
    assert!(
        !shown.contains("hunter2") && !shown.contains("db.internal"),
        "{shown}"
    );
}
