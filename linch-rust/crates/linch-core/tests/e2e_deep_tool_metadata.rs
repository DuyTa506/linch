//! What a tool returns for the host (its metadata, a failure's details) goes
//! into the call's `tool_call_end` event, which the stores must read back
//! whatever nesting a tool gives it.

mod common;

use std::sync::Arc;

use async_trait::async_trait;
use common::flaky::Flaky;
use common::*;
use linch_core::{
    Envelope, Event, JsonObject, JsonValue, PermissionMode, RunOptions, Tool, ToolContext,
    ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope,
};

/// A tool whose metadata (or, when it fails, its details) nests `objects`
/// objects.
struct Deep {
    schema: JsonObject,
    objects: usize,
    fails: bool,
}

fn nested(objects: usize) -> JsonObject {
    let text = format!("{}1{}", "{\"a\":".repeat(objects), "}".repeat(objects));
    serde_json::from_str(&text).unwrap()
}

#[async_trait]
impl Tool for Deep {
    fn name(&self) -> &str {
        "Deep"
    }
    fn description(&self) -> &str {
        "Deep."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let deep = nested(self.objects);
        if self.fails {
            return Err(ToolOutputError {
                message: "failed".into(),
                code: None,
                details: Some(JsonValue::Object(deep)),
            });
        }
        let mut output = ToolOutput::new("ok");
        output.metadata = deep;
        Ok(output)
    }
}

/// The `tool_call_end` of one run of `Deep`, as the store text reads back.
async fn end_reads_back(objects: usize, fails: bool) -> bool {
    let schema = serde_json::json!({"type": "object"});
    let schema = schema.as_object().unwrap().clone();
    let registry = ToolRegistry::new();
    let _ = registry
        .register(Arc::new(Deep {
            schema,
            objects,
            fails,
        }))
        .unwrap();
    let turns = vec![
        linch_core::provider::scripted::TurnBuilder::new(MODEL)
            .tool_call("c1", "Deep", &serde_json::json!({}))
            .build(),
        text_turn("done"),
    ];
    let provider = Arc::new(Flaky::new(turns));
    let stores = Arc::new(Stores::default());
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec(), provider, Arc::new(registry), permissions, stores);
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
    let end: Vec<&Envelope> = (envelopes.iter())
        .filter(|e| matches!(e.event, Event::ToolCallEnd(_)))
        .collect();
    let stored = end[0].to_value().unwrap().to_string();
    stored.parse::<Envelope>().is_ok()
}

#[tokio::test]
async fn a_tool_end_with_deep_metadata_reads_back() {
    let limit = linch_core::JOURNAL_MAX_DEPTH + 1;
    assert!(end_reads_back(limit, false).await, "at the limit");
    assert!(end_reads_back(127, false).await, "past the limit");
}

#[tokio::test]
async fn a_tool_end_with_deep_failure_details_reads_back() {
    let limit = linch_core::JOURNAL_MAX_DEPTH + 1;
    assert!(end_reads_back(limit, true).await, "at the limit");
    assert!(end_reads_back(127, true).await, "past the limit");
}
