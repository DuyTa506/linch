//! A tool call whose input nests as deep as the model's output parser
//! takes must leave a run that every store read, cancel and fail can still
//! load: the journal, the checkpoint and the session read back what they
//! hold.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, JsonObject, PermissionEngine, PermissionMode, PermissionPolicy,
    RunId, RunOptions, RunStatus, RunStore, ScriptedProvider, Tool, ToolContext, ToolOutput,
    ToolRegistry, ToolResult, ToolScope,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use serde_json::json;

use crate::common::TestDb;

const MODEL: &str = "scripted-model";
const NS: &str = "ns";

/// A write tool that echoes nothing: every call of it asks for approval.
struct Write(JsonObject);

#[async_trait]
impl Tool for Write {
    fn name(&self) -> &str {
        "Write"
    }
    fn description(&self) -> &str {
        "Write."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.0
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new(json!("ok")))
    }
}

/// `depth` objects nested in one another around a number.
fn nested(depth: usize) -> serde_json::Value {
    let text = format!("{}1{}", "{\"a\":".repeat(depth), "}".repeat(depth));
    serde_json::from_str(&text).expect("the output parser takes it")
}

/// A run, on Postgres, whose first response holds a call with a `depth`
/// deep input next to an ordinary write call that asks for approval, so the
/// run suspends with that response in its checkpoint.
async fn suspended(db: &TestDb, depth: usize) -> (linch_core::Agent, RunId) {
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let registry = ToolRegistry::new();
    let _ = registry.register(Arc::new(Write(schema))).unwrap();
    let turn = TurnBuilder::new(MODEL)
        .text("working")
        .tool_call("deep", "Write", &nested(depth))
        .tool_call("plain", "Write", &json!({"a": 1}))
        .build();
    let provider = Arc::new(ScriptedProvider::new([
        turn,
        TurnBuilder::new(MODEL).text("done").build(),
    ]));
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider)
        .tools(Arc::new(registry))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(PgRunStore::new(db.pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(db.pool.clone())))
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let mut last = None;
    let mut stream = stream;
    while let Some(item) = futures::StreamExt::next(&mut stream).await {
        last = Some(item.expect("the run's stream does not fail").event);
    }
    assert!(matches!(last, Some(Event::Suspended(_))), "{last:?}");
    (agent, run_id)
}

/// A run suspended on a call with a `depth` deep input reads back and
/// cancels, and its session takes the next run.
async fn reads_back_and_cancels(name: &str, depth: usize) {
    let Some(db) = TestDb::migrated(name).await else {
        return;
    };
    let (agent, run_id) = suspended(&db, depth).await;
    let store = PgRunStore::new(db.pool.clone());
    let events = store.events(NS, &run_id, 0).await.map(|e| e.len());
    let record = store.load(NS, &run_id).await.map(|r| r.status);
    let cancelled = agent.cancel(run_id.clone(), RunOptions::new("s1")).await;
    let status = agent.status(&run_id).await;
    assert!(
        events.is_ok() && record.is_ok() && matches!(cancelled, Ok(true)),
        "events {events:?}, load {record:?}, cancel {cancelled:?}, status after {status:?}"
    );
    assert_eq!(status.unwrap(), RunStatus::Aborted);
    let mut next = agent.run("again", RunOptions::new("s1"));
    while let Some(item) = futures::StreamExt::next(&mut next).await {
        item.expect("the next run on the session works");
    }
    db.finish().await;
}

/// The deepest input the model's output parser takes.
#[tokio::test]
async fn a_run_with_a_deepest_tool_input_reads_back_and_cancels() {
    reads_back_and_cancels("deep_input_cancel", 127).await;
}

/// The deepest input the journal keeps as a call.
#[tokio::test]
async fn a_run_with_a_tool_input_at_the_journal_limit_reads_back_and_cancels() {
    let limit = linch_core::JOURNAL_MAX_DEPTH + 1;
    reads_back_and_cancels("deep_input_limit", limit).await;
}

#[tokio::test]
async fn a_run_with_a_deepest_tool_input_can_be_failed() {
    let Some(db) = TestDb::migrated("deep_input_fail").await else {
        return;
    };
    let (agent, run_id) = suspended(&db, 127).await;
    let failed = agent
        .fail(run_id.clone(), "give up", RunOptions::new("s1"))
        .await;
    assert!(failed.is_ok(), "{failed:?}");
    assert_eq!(agent.status(&run_id).await.unwrap(), RunStatus::Failed);
    db.finish().await;
}
