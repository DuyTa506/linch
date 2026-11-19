//! A tool's idempotency key is the same for the same call however a run is
//! crashed and resumed: `run_id:tool_use_id`, the key `Bash` hands a remote
//! host at `exec.start`. A crash at any write leaves it unchanged.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    JsonObject, PermissionMode, RunOptions, RunStore, Tool, ToolContext, ToolOutput, ToolResult,
    ToolScope,
};
use serde_json::json;

/// Records the key of every call it serves.
struct Keyed {
    schema: JsonObject,
    keys: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl Tool for Keyed {
    fn name(&self) -> &str {
        "Write"
    }
    fn description(&self) -> &str {
        "Record the key."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }
    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        self.keys.lock().unwrap().push(ctx.idempotency_key());
        Ok(ToolOutput::new(json!("ok")))
    }
}

fn tools(keys: &Arc<Mutex<Vec<String>>>) -> Arc<linch_core::ToolRegistry> {
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let registry = linch_core::ToolRegistry::new();
    let keyed = Keyed {
        schema,
        keys: Arc::clone(keys),
    };
    let _ = registry.register(Arc::new(keyed)).unwrap();
    Arc::new(registry)
}

fn turns() -> Vec<linch_core::ScriptedTurn> {
    vec![tool_turn(0, 1, "Write"), text_turn("done")]
}

#[tokio::test]
async fn a_crash_at_any_write_keeps_the_key_of_the_call() {
    let clean = Arc::new(Stores::default());
    let keys = Arc::new(Mutex::new(Vec::new()));
    let baseline = agent(
        Arc::new(Replay::new(turns())),
        tools(&keys),
        policy(PermissionMode::SkipDangerous),
        clean.clone(),
    );
    finish(baseline.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    assert_eq!(keys.lock().unwrap().len(), 1);
    let mut served = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let keys = Arc::new(Mutex::new(Vec::new()));
            let provider = Arc::new(Replay::new(turns()));
            let permissions = || policy(PermissionMode::SkipDangerous);
            let crashing = agent(provider.clone(), tools(&keys), permissions(), faulty);
            let stream = crashing.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}: the run stops");
            let Ok(record) = stores.runs.load(NS, &run_id).await else {
                continue;
            };
            if !record.status.is_terminal() {
                let again = agent(provider, tools(&keys), permissions(), stores.clone());
                finish(again.resume(run_id.clone(), RunOptions::new("s1"))).await;
            }
            let expected = format!("{run_id}:call_0_0");
            for key in keys.lock().unwrap().iter() {
                served += 1;
                assert_eq!(key, &expected, "{context}");
            }
        }
    }
    assert!(served > 0, "some crash point still ran the call");
}
