//! A model stuck on one call whose result is offloaded still ends the run
//! after three identical calls, whatever file name each offload gets.

use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, Hooks, JsonObject, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent,
    ProviderRequest, ProviderStream, Role, RunOptions, Tool, ToolContext, ToolOutput, ToolRegistry,
    ToolResult, ToolScope,
};
use linch_tools::vfs::{MemorySessions, OffloadConfig, ResultOffload, SessionFiles};
use serde_json::json;
use tokio_util::sync::CancellationToken;

const MODEL: &str = "scripted-model";

struct Big {
    schema: JsonObject,
}

#[async_trait]
impl Tool for Big {
    fn name(&self) -> &str {
        "big"
    }
    fn description(&self) -> &str {
        "Return a large result."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let all: Vec<String> = (1..=2000).map(|i| format!("line {i}")).collect();
        Ok(ToolOutput::new(all.join("\n")))
    }
}

/// Asks for `big` with the call id `id(n)` on every turn.
struct Stuck(fn(usize) -> String);

#[async_trait]
impl Provider for Stuck {
    fn id(&self) -> &str {
        "stuck"
    }
    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let n = (request.messages.iter())
            .filter(|message| message.role == Role::Assistant)
            .count();
        let turn = match n {
            0..=11 => TurnBuilder::new(MODEL)
                .tool_call(&(self.0)(n), "big", &json!({}))
                .build(),
            _ => TurnBuilder::new(MODEL).text("done").build(),
        };
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            turn.events.into_iter().map(Ok).collect();
        let stream: BoxStream<'static, _> = futures::stream::iter(events).boxed();
        Ok(stream)
    }
}

/// How many times `big` ran and the loop guard's reasons, for a model that
/// repeats one call whose result is offloaded.
async fn stuck(id: fn(usize) -> String) -> (usize, Vec<String>) {
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    let tools = ToolRegistry::new();
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let _big = tools.register(Arc::new(Big { schema })).unwrap();
    let hooks = Hooks::default();
    let offload = ResultOffload::new(files, OffloadConfig::new(1_000)).unwrap();
    let _offload = hooks.post_tool_use.register(Arc::new(offload)).unwrap();
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::new(Stuck(id)))
        .tools(Arc::new(tools))
        .hooks(Arc::new(hooks))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("t")
        .build()
        .unwrap();
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let (mut runs, mut guards) = (0, Vec::new());
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::ToolCallEnd(_) => runs += 1,
            Event::LoopGuard(guard) => {
                let reason = serde_json::to_value(&guard.reason).unwrap();
                guards.push(reason.as_str().unwrap().to_owned());
            }
            _ => {}
        }
    }
    (runs, guards)
}

fn stops_after_three(outcome: (usize, Vec<String>)) {
    let (runs, guards) = outcome;
    assert_eq!(
        (runs, guards.as_slice()),
        (3, ["repeated_tool_call".to_owned()].as_slice())
    );
}

#[tokio::test]
async fn a_repeated_offloaded_call_with_plain_ids_is_a_loop() {
    stops_after_three(stuck(|n| format!("call_{n}")).await);
}

#[tokio::test]
async fn a_repeated_offloaded_call_with_a_repeated_call_id_is_a_loop() {
    stops_after_three(stuck(|_| "call_0".to_owned()).await);
}

#[tokio::test]
async fn a_repeated_offloaded_call_with_an_unusual_call_id_is_a_loop() {
    stops_after_three(stuck(|n| format!("call:{n}/é{}", "x".repeat(100))).await);
}
