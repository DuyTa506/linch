//! What a run reports about itself beyond its answer (17.2): when each
//! envelope was made, how long the call that ended the run took, the
//! context each provider request was built from, and each tool's metadata.

mod common;

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use common::*;
use linch_core::events::kinds::ContextBuildEvent;
use linch_core::{
    Envelope, Event, JsonObject, PermissionMode, RunOptions, RunStore, ScriptedProvider, Tool,
    ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
};

/// Each primary request gets one durable `context_build`, staged with the
/// call's delivery so it costs no write of its own.
#[tokio::test]
async fn every_provider_request_reports_its_context() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Slow")));
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent(provider.clone(), slow_tools(), permissions, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let builds: Vec<&ContextBuildEvent> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ContextBuild(build) => Some(build),
            _ => None,
        })
        .collect();
    let requests = provider.requests();
    assert_eq!(builds.len(), requests.len());
    for (build, request) in builds.iter().zip(&requests) {
        assert_eq!(build.system_blocks, request.system.len() as u64);
        assert_eq!(build.messages, request.messages.len() as u64);
        let tools: Vec<String> = request.tools.iter().map(|t| t.name.clone()).collect();
        assert_eq!(build.selected_tools.as_ref(), Some(&tools));
        assert!(build.budget.is_empty() && build.metadata.is_empty());
    }
    assert_eq!(
        builds.iter().map(|b| b.messages).collect::<Vec<_>>(),
        [1, 3]
    );
    // Durable, and in the write that holds the call's delivery.
    let stored = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let durable = stored
        .iter()
        .filter(|e| matches!(e.event, Event::ContextBuild(_)));
    assert_eq!(durable.count(), 2);
    let writes = stores.log.lock().unwrap().clone();
    let with_delivery = writes.iter().filter(|write| {
        matches!(write, Write::Commit(kinds)
            if kinds == &["context_build".to_owned(), "model_call_delivery".to_owned()])
    });
    assert_eq!(with_delivery.count(), 2, "{writes:?}");
}

/// A tool's output metadata is on its durable end event, and the model
/// never sees it.
#[tokio::test]
async fn a_tools_metadata_rides_its_end_event_and_not_the_request() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Slow")));
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent(provider.clone(), slow_tools(), permissions, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let streamed = finish(stream).await;
    let stored = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let rows = object(serde_json::json!({"rows": 3}));
    for envelopes in [&streamed, &stored] {
        let ends: Vec<&JsonObject> = (envelopes.iter())
            .filter_map(|e| match &e.event {
                Event::ToolCallEnd(end) => end.metadata.as_deref(),
                _ => None,
            })
            .collect();
        assert_eq!(ends, [&rows]);
    }
    let requests = provider.requests();
    let last = serde_json::to_string(&requests.last().unwrap().messages).unwrap();
    assert!(last.contains("done") && !last.contains("rows"), "{last}");
}

/// A read tool that takes `PAUSE` to answer, with metadata for the host.
struct Slow(JsonObject);

const PAUSE: Duration = Duration::from_millis(30);

#[async_trait]
impl Tool for Slow {
    fn name(&self) -> &str {
        "Slow"
    }
    fn description(&self) -> &str {
        "Answer after a pause."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.0
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        tokio::time::sleep(PAUSE).await;
        let mut output = ToolOutput::new("done");
        output.metadata = object(serde_json::json!({"rows": 3}));
        Ok(output)
    }
}

fn slow_tools() -> Arc<ToolRegistry> {
    let tools = ToolRegistry::new();
    let schema = serde_json::json!({"type": "object"});
    let _ = tools.register(Arc::new(Slow(object(schema)))).unwrap();
    Arc::new(tools)
}

fn now_ms() -> u64 {
    let since = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    u64::try_from(since.as_millis()).unwrap()
}

#[tokio::test]
async fn every_envelope_has_a_timestamp_and_the_result_a_duration() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(1, 1, "Slow")));
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent(provider, slow_tools(), permissions, stores.clone());
    let before = now_ms();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let after = now_ms();

    for envelope in &envelopes {
        let at = envelope.ts_ms;
        assert!(
            (before..=after).contains(&at),
            "{at} outside {before}..={after}"
        );
    }
    let stored: Vec<Envelope> = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let streamed: Vec<u64> = (envelopes.iter())
        .filter(|e| matches!(e.event, Event::Result(_)))
        .map(|e| e.ts_ms)
        .collect();
    let kept = stored.last().map(|e| e.ts_ms);
    assert_eq!(streamed.first().copied(), kept, "the store keeps ts_ms");

    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    let pause = u64::try_from(PAUSE.as_millis()).unwrap();
    assert!(result.duration_ms >= pause, "{} ms", result.duration_ms);
    assert!(
        result.duration_ms <= after - before,
        "{} ms",
        result.duration_ms
    );
}
