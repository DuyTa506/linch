use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{ContextBuild, ContextBuildRequest};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::vfs::{FileBackend, VfsError};
use linch_core::{
    AgentBuilder, AgentSpec, ContentBlock, Hook, Hooks, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, Role, RunId, RunOptions, ScriptedProvider,
    SessionId, TextBlock,
};
use linch_tools::vfs::{MemorySessions, SessionFiles};

use super::super::*;
use crate::test_support::drain;

fn request(session: &str) -> ContextBuildRequest {
    ContextBuildRequest {
        session_id: SessionId::new(session),
        run_id: RunId::new("r1"),
        messages: Arc::new(Vec::new()),
    }
}

fn texts(blocks: Vec<TextBlock>) -> Vec<String> {
    blocks.into_iter().map(|block| block.text).collect()
}

/// A hook over per-session memory files, and those files.
fn hook() -> (FileMemoryContext, Arc<MemorySessions>) {
    let sessions = Arc::new(MemorySessions::new());
    let files = Arc::clone(&sessions);
    (FileMemoryContext::new(move |s| files.backend(s)), sessions)
}

#[tokio::test]
async fn without_an_index_only_the_instruction_is_shown() {
    let (hook, _) = hook();
    let response = hook.call(&request("s1")).await.unwrap();
    let python = "Memories live under /memories/, one file per fact. MEMORY.md is a \
                  one-line-per-file index. Use the file tools to read and maintain memories; \
                  update or delete stale facts.";
    assert_eq!(texts(response.blocks), [python]);
    assert_eq!(
        <FileMemoryContext as Hook<ContextBuild>>::policy_id(&hook),
        "linch.deep-agent.file-memory-context"
    );
    assert_eq!(hook.policy_version(), 1);
}

#[tokio::test]
async fn the_index_of_the_session_follows_the_instruction() {
    let (hook, sessions) = hook();
    let index = "- [Style](style.md) - answers in French\n- [Repo](repo.md) - uses cargo";
    let backend = sessions.backend(&SessionId::new("s1"));
    backend.write(MEMORY_INDEX, index).await.unwrap();
    let response = hook.call(&request("s1")).await.unwrap();
    let expected = format!("/memories/MEMORY.md:\n{index}");
    assert_eq!(texts(response.blocks), [MEMORY_INSTRUCTION, &expected]);
    let other = hook.call(&request("s2")).await.unwrap();
    assert_eq!(other.blocks.len(), 1, "each session has its own files here");
}

#[tokio::test]
async fn a_long_index_is_cut_to_200_lines() {
    let (hook, sessions) = hook();
    let lines: Vec<String> = (1..=201).map(|n| format!("- fact {n}")).collect();
    let backend = sessions.backend(&SessionId::new("s1"));
    backend
        .write(MEMORY_INDEX, &lines.join("\n"))
        .await
        .unwrap();
    let response = hook.call(&request("s1")).await.unwrap();
    let shown = &response.blocks[1].text;
    let expected = format!(
        "/memories/MEMORY.md:\n{}\n[MEMORY.md truncated to 200 lines.]",
        lines[..200].join("\n")
    );
    assert_eq!(*shown, expected);
    assert_eq!(MEMORY_INDEX_LINES, 200);

    backend
        .write(MEMORY_INDEX, &lines[..200].join("\n"))
        .await
        .unwrap();
    let exact = hook.call(&request("s1")).await.unwrap();
    assert!(!exact.blocks[1].text.contains("truncated"), "200 lines fit");
}

/// A backend whose every call fails.
struct Broken;

#[async_trait]
impl FileBackend for Broken {
    async fn read(&self, _: &str) -> Result<String, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn write(&self, _: &str, _: &str) -> Result<(), VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn edit(&self, _: &str, _: &str, _: &str, _: bool) -> Result<usize, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn ls(&self, _: &str) -> Result<Vec<String>, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn exists(&self, _: &str) -> Result<bool, VfsError> {
        Err(VfsError::Backend("down".into()))
    }
    async fn delete(&self, _: &str) -> Result<(), VfsError> {
        Err(VfsError::Backend("down".into()))
    }
}

#[tokio::test]
async fn a_failed_read_is_an_error_for_the_chain() {
    let hook = FileMemoryContext::new(|_| Arc::new(Broken) as Arc<dyn FileBackend>);
    let error = hook.call(&request("s1")).await.unwrap_err();
    assert!(error.to_string().contains("down"), "{error}");
}

#[tokio::test]
async fn every_request_carries_the_index_after_the_prompt() {
    let (hook, sessions) = hook();
    let backend = sessions.backend(&SessionId::new("s1"));
    backend
        .write(MEMORY_INDEX, "- [Style](style.md)")
        .await
        .unwrap();
    let hooks = Arc::new(Hooks::default());
    let _registration = hooks.context_build.register(Arc::new(hook)).unwrap();
    let provider = Arc::new(ScriptedProvider::new([TurnBuilder::new("m")
        .text("hi")
        .build()]));
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(AgentSpec::new("m"))
        .provider(provider.clone())
        .hooks(hooks)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("deep")
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    drain(stream).await;
    let requests = provider.requests();
    let last = requests[0].messages.last().unwrap();
    assert_eq!(last.role, Role::User);
    let shown: Vec<&str> = (last.content.iter())
        .filter_map(|block| match block {
            ContentBlock::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(
        shown,
        [
            "go",
            MEMORY_INSTRUCTION,
            "/memories/MEMORY.md:\n- [Style](style.md)"
        ]
    );
    let log = agent.events(&run_id, 0).await.unwrap();
    let logged = serde_json::to_string(&log).unwrap();
    assert!(!logged.contains("Style"), "the index is request-only");
}
