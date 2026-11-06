//! Fixtures shared by the unit tests: an agent on a scripted provider and
//! memory stores, and a test tool.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, JsonObject, JsonValue, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, ScriptedProvider, ScriptedTurn, Tool,
    ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolScope, Usage,
};
use serde_json::json;

pub const MODEL: &str = "scripted-eval";
pub const NS: &str = "evals";

pub fn usage(input: u64, output: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    }
}

/// A text answer reporting 10 input and 5 output tokens.
pub fn text_turn(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text(text)
        .usage(usage(10, 5))
        .build()
}

/// One call of `tool` with `input`, reporting 10 input and 5 output tokens.
pub fn tool_turn(id: &str, tool: &str, input: JsonValue) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(id, tool, &input)
        .usage(usage(10, 5))
        .build()
}

/// A tool that answers `ok`, or fails when its input has `"fail": true`.
pub struct Probe {
    name: &'static str,
    scope: ToolScope,
    schema: JsonObject,
}

impl Probe {
    fn new(name: &'static str, scope: ToolScope) -> Self {
        let schema = json!({"type": "object", "properties": {"fail": {"type": "boolean"}}});
        Self {
            name,
            scope,
            schema: schema.as_object().cloned().unwrap_or_default(),
        }
    }
}

#[async_trait]
impl Tool for Probe {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Answer ok, or fail on request."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        self.scope
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        if input.get("fail") == Some(&JsonValue::Bool(true)) {
            return Err(ToolOutputError::new("probe failed"));
        }
        Ok(ToolOutput::new("ok"))
    }
}

/// An agent on `provider` and memory stores, with a read tool `Probe`
/// that runs at once and a write tool `Edit` that asks for approval.
pub struct Fixture {
    pub agent: Agent,
    pub provider: Arc<ScriptedProvider>,
    /// How many sessions the agent was told to close.
    pub closed: Arc<AtomicUsize>,
}

pub fn scripted(turns: Vec<ScriptedTurn>) -> Fixture {
    let provider = Arc::new(ScriptedProvider::new(turns));
    let tools = ToolRegistry::new();
    for tool in [
        Probe::new("Probe", ToolScope::Read),
        Probe::new("Edit", ToolScope::Write),
    ] {
        // The registry lives as long as the agent; the handle is not needed.
        let _ = tools.register(Arc::new(tool)).unwrap();
    }
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let closed = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&closed);
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider.clone())
        .tools(Arc::new(tools))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace(NS)
        .on_session_close(move |_| {
            counter.fetch_add(1, Ordering::SeqCst);
        })
        .build()
        .unwrap();
    Fixture {
        agent,
        provider,
        closed,
    }
}
