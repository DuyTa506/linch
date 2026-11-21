//! The log of a tool cache that stops for a failed clear. Alone in its file:
//! the subscriber is this process's, since a thread-local one misses events
//! from other threads, and a callsite first met by a thread without one stays
//! off for the whole process, which other tests in one binary would trigger.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Hooks, InMemoryToolCache, Lookup, LoopGuard, PermissionMode,
    PutLimits, RunOptions, ScriptedTurn, Tool, ToolCache, ToolCacheError, ToolCacheStore,
    ToolOutput, ToolRegistry, ToolScope,
};
use serde_json::json;

/// A response calling `tool` with the same input every time.
fn call(turn: usize, tool: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text("working")
        .tool_call(&format!("call_{turn}"), tool, &json!({"value": "same"}))
        .usage(usage(10, 5))
        .build()
}

fn script(tools: &[&str]) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = tools.iter().enumerate().map(|(n, t)| call(n, t)).collect();
    turns.push(text_turn("done"));
    turns
}

/// A spec that caches `tools`, with the loop guard's repeat check off so a
/// run may repeat a call.
fn cached(tools: &[&str]) -> AgentSpec {
    let mut spec = spec();
    spec.tool_cache = Some(ToolCache::new(tools.iter().copied()));
    spec.loop_guard = LoopGuard {
        max_identical_tool_calls: 0,
        ..LoopGuard::default()
    };
    spec
}

/// A store whose clear always fails, over an in-memory one.
struct FailingClear(InMemoryToolCache);

#[async_trait]
impl ToolCacheStore for FailingClear {
    async fn get(&self, namespace: &str, key: &str) -> Result<Lookup, ToolCacheError> {
        self.0.get(namespace, key).await
    }
    async fn put(
        &self,
        namespace: &str,
        key: &str,
        output: &ToolOutput,
        epoch: u64,
        limits: &PutLimits,
    ) -> Result<bool, ToolCacheError> {
        self.0.put(namespace, key, output, epoch, limits).await
    }
    async fn clear(&self, _namespace: &str) -> Result<(), ToolCacheError> {
        Err(ToolCacheError("injected".into()))
    }
}

/// A `tracing` writer that keeps what it is given.
#[derive(Clone, Default)]
struct Captured(Arc<Mutex<Vec<u8>>>);

impl std::io::Write for Captured {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Captured {
    type Writer = Captured;
    fn make_writer(&'a self) -> Captured {
        self.clone()
    }
}

/// Caching that stops for a failed clear says so, once, in the log.
#[tokio::test]
async fn caching_that_stops_for_a_failed_clear_is_logged_once() {
    let captured = Captured::default();
    let subscriber = tracing_subscriber::fmt()
        .with_writer(captured.clone())
        .with_ansi(false)
        .finish();
    tracing::subscriber::set_global_default(subscriber).unwrap();

    let shared: Arc<dyn ToolCacheStore> = Arc::new(FailingClear(InMemoryToolCache::new()));
    let runs = Runs::default();
    let registry = ToolRegistry::new();
    let tools: Vec<Arc<dyn Tool>> = vec![
        Arc::new(Echo::new("Read", ToolScope::Read, &runs)),
        Arc::new(Echo::new("Write", ToolScope::Write, &runs)),
    ];
    for tool in tools {
        let _ = registry.register(tool).unwrap();
    }
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(cached(&["Read"]))
        .provider(Arc::new(Replay::new(script(&["Write", "Write", "Read"]))))
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(Hooks::default()))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .tool_cache_store(shared)
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let log = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
    let lines = log.matches("the tool cache stopped for an agent").count();
    assert_eq!(lines, 1, "{log}");
    assert!(log.contains("WARN"), "{log}");
}
