//! Scheduler tests. Shared fixtures live here; planning cases are in
//! `tests/plan.rs`, batching and abort in `tests/execute.rs`, results in
//! `tests/results.rs`, and property tests in `tests/props.rs`.

mod execute;
mod plan;
mod props;
mod results;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::FutureExt;
use futures::future::BoxFuture;
use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::*;
use crate::canonical::to_canonical_string;
use crate::hooks::{CallbackError, Hook, Hooks, Point};
use crate::ids::{RunId, SessionId};
use crate::json::{JsonObject, JsonValue};
use crate::message::{ToolResultBlock, ToolUseBlock};
use crate::permissions::{PermissionEngine, PermissionMode, PermissionPolicy};
use crate::registry::ToolRegistry;
use crate::tool::{
    Tool, ToolContext, ToolOutput, ToolOutputError, ToolProgress, ToolResult, ToolScope,
};

/// Lines appended by tools, hooks and sinks, in the order they happened.
type Log = Arc<Mutex<Vec<String>>>;

fn new_log() -> Log {
    Arc::default()
}

fn push(log: &Log, line: impl Into<String>) {
    log.lock().unwrap().push(line.into());
}

fn lines(log: &Log) -> Vec<String> {
    log.lock().unwrap().clone()
}

fn obj(value: JsonValue) -> JsonObject {
    match value {
        JsonValue::Object(map) => map,
        other => panic!("not an object: {other}"),
    }
}

fn use_block(id: &str, name: &str, input: JsonValue) -> ToolUseBlock {
    ToolUseBlock {
        id: id.to_owned(),
        name: name.to_owned(),
        input: obj(input),
    }
}

type Body = Arc<dyn Fn(JsonObject, ToolContext) -> BoxFuture<'static, ToolResult> + Send + Sync>;

/// A tool whose `execute` runs a closure. It logs `exec <name>` when it
/// runs and, given a validate log, `validate <name> <input>`.
struct TestTool {
    name: String,
    scope: ToolScope,
    schema: JsonObject,
    output_schema: Option<JsonObject>,
    log: Log,
    validate_log: Option<Log>,
    body: Body,
}

impl TestTool {
    /// A tool that echoes its input as the value.
    fn new(name: &str, scope: ToolScope, log: &Log) -> Self {
        Self {
            name: name.to_owned(),
            scope,
            schema: obj(json!({"type": "object"})),
            output_schema: None,
            log: Arc::clone(log),
            validate_log: None,
            body: Arc::new(|input, _ctx| {
                async move { Ok(ToolOutput::new(JsonValue::Object(input))) }.boxed()
            }),
        }
    }

    fn body<F>(mut self, body: F) -> Self
    where
        F: Fn(JsonObject, ToolContext) -> BoxFuture<'static, ToolResult> + Send + Sync + 'static,
    {
        self.body = Arc::new(body);
        self
    }

    fn schema(mut self, schema: JsonValue) -> Self {
        self.schema = obj(schema);
        self
    }

    fn output_schema(mut self, schema: JsonValue) -> Self {
        self.output_schema = Some(obj(schema));
        self
    }

    fn log_validate(mut self, log: &Log) -> Self {
        self.validate_log = Some(Arc::clone(log));
        self
    }
}

#[async_trait]
impl Tool for TestTool {
    fn name(&self) -> &str {
        &self.name
    }
    fn description(&self) -> &str {
        "test tool"
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        self.scope
    }
    fn output_schema(&self) -> Option<&JsonObject> {
        self.output_schema.as_ref()
    }
    fn validate(&self, input: JsonObject) -> Result<JsonObject, ToolOutputError> {
        if let Some(log) = &self.validate_log {
            let text = to_canonical_string(&JsonValue::Object(input.clone())).unwrap();
            push(log, format!("validate {} {text}", self.name));
        }
        Ok(input)
    }
    fn summarize(&self, input: &JsonObject) -> String {
        format!("{} with {} fields", self.name, input.len())
    }
    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        push(&self.log, format!("exec {}", self.name));
        (self.body)(input, ctx).await
    }
}

type Answer<P> = Box<dyn Fn(&<P as Point>::Request) -> <P as Point>::Response + Send + Sync>;

/// A hook that logs `hook <id>` and answers with a closure.
struct FnHook<P: Point> {
    id: &'static str,
    log: Log,
    answer: Answer<P>,
}

fn hook<P: Point>(
    id: &'static str,
    log: &Log,
    answer: impl Fn(&P::Request) -> P::Response + Send + Sync + 'static,
) -> Arc<dyn Hook<P>> {
    Arc::new(FnHook {
        id,
        log: Arc::clone(log),
        answer: Box::new(answer),
    })
}

#[async_trait]
impl<P: Point> Hook<P> for FnHook<P> {
    fn policy_id(&self) -> &str {
        self.id
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(&self, request: &P::Request) -> Result<P::Response, CallbackError> {
        push(&self.log, format!("hook {}", self.id));
        Ok((self.answer)(request))
    }
}

/// The agent pieces one batch needs.
struct Fixture {
    registry: ToolRegistry,
    hooks: Hooks,
    engine: PermissionEngine,
    run: RunIdentity,
}

impl Fixture {
    fn new(mode: PermissionMode) -> Self {
        Self::with_policy(PermissionPolicy::new(mode, "/project"))
    }

    fn with_policy(policy: PermissionPolicy) -> Self {
        Self {
            registry: ToolRegistry::new(),
            hooks: Hooks::default(),
            engine: PermissionEngine::new(policy).unwrap(),
            run: RunIdentity {
                session_id: SessionId::from("s1"),
                run_id: RunId::from("r1"),
                principal: None,
                trace_id: None,
            },
        }
    }

    fn tool(&self, tool: TestTool) {
        // Never disposed: the tool stays for the registry's lifetime.
        let _registration = self.registry.register(Arc::new(tool)).unwrap();
    }

    async fn plan(&self, blocks: &[ToolUseBlock]) -> BatchPlan {
        self.plan_with(blocks, &HashMap::new()).await
    }

    async fn plan_with(
        &self,
        blocks: &[ToolUseBlock],
        approvals: &HashMap<String, RecordedApproval>,
    ) -> BatchPlan {
        let tools = self.registry.snapshot();
        plan(
            blocks,
            &tools,
            &self.engine,
            &self.hooks,
            &self.run,
            approvals,
        )
        .await
    }

    async fn execute(
        &self,
        plan: BatchPlan,
        sink: &RecSink,
        options: &ExecuteOptions,
    ) -> Result<Vec<ToolResultBlock>, SinkDown> {
        execute(plan, sink, &self.hooks, &self.run, options).await
    }

    /// Plan `blocks` and execute them with `max_concurrency`.
    async fn run(
        &self,
        blocks: &[ToolUseBlock],
        sink: &RecSink,
        max: usize,
    ) -> Vec<ToolResultBlock> {
        let plan = self.plan(blocks).await;
        self.execute(plan, sink, &options(max)).await.unwrap()
    }
}

fn options(max_concurrency: usize) -> ExecuteOptions {
    ExecuteOptions {
        max_concurrency,
        call_timeout: None,
        cancel: CancellationToken::new(),
    }
}

/// The sink's error.
#[derive(Debug, PartialEq, Eq)]
struct SinkDown;

/// A sink that logs `start <id>` and `end <id>`, keeps each outcome and
/// progress message, and can fail `tool_starting` for one call.
#[derive(Default)]
struct RecSink {
    log: Log,
    fail_start: Option<&'static str>,
    finished: Mutex<Vec<(String, CallOutcome)>>,
    progress: Mutex<Vec<(String, String)>>,
}

impl RecSink {
    fn logging(log: &Log) -> Self {
        Self {
            log: Arc::clone(log),
            ..Self::default()
        }
    }

    fn outcome(&self, id: &str) -> CallOutcome {
        let finished = self.finished.lock().unwrap();
        let mut matches = finished.iter().filter(|(done, _)| done == id);
        let (_, outcome) = matches.next().expect("call finished");
        assert!(matches.next().is_none(), "{id} finished twice");
        outcome.clone()
    }

    fn finished_ids(&self) -> Vec<String> {
        let finished = self.finished.lock().unwrap();
        finished.iter().map(|(id, _)| id.clone()).collect()
    }
}

#[async_trait]
impl ToolSink for RecSink {
    type Error = SinkDown;

    async fn tool_starting(&self, call: &PlannedCall) -> Result<(), SinkDown> {
        push(&self.log, format!("start {}", call.tool_use_id));
        if self.fail_start == Some(call.tool_use_id.as_str()) {
            return Err(SinkDown);
        }
        Ok(())
    }

    async fn tool_finished(
        &self,
        call: &PlannedCall,
        outcome: &CallOutcome,
    ) -> Result<(), SinkDown> {
        push(&self.log, format!("end {}", call.tool_use_id));
        let entry = (call.tool_use_id.clone(), outcome.clone());
        self.finished.lock().unwrap().push(entry);
        Ok(())
    }

    fn tool_progress(&self, call: &PlannedCall, progress: ToolProgress) {
        let entry = (call.tool_use_id.clone(), progress.message);
        self.progress.lock().unwrap().push(entry);
    }
}

/// Text of a one-part result block.
fn text(block: &ToolResultBlock) -> &str {
    match block.content.as_slice() {
        [crate::message::ToolResultPart::Text(part)] => &part.text,
        other => panic!("expected one text part, got {other:?}"),
    }
}
