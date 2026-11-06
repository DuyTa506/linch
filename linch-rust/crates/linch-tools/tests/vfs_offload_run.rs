//! Offload inside a real run: the pointer, not the payload, is what the
//! session log, the events and the next provider request hold, and the
//! model reads the payload back with `read_file`.

use std::sync::Arc;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Hooks, JsonObject, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, SessionId,
    SessionStore, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope,
};
use linch_tools::vfs::{MemorySessions, OffloadConfig, ResultOffload, SessionFiles, vfs_tools};
use serde_json::json;

const MODEL: &str = "scripted-model";

/// Returns 2000 numbered lines; the last is the marker the model must not
/// see inline.
struct Big {
    schema: JsonObject,
}

fn payload() -> String {
    let all: Vec<String> = (1..=2000).map(|i| format!("line {i}")).collect();
    all.join("\n")
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
        Ok(ToolOutput::new(payload()))
    }
}

#[tokio::test]
async fn the_logged_result_is_the_pointer_and_reads_back() {
    let path = "/offload/big_call_1.txt";
    let provider = Arc::new(ScriptedProvider::new([
        TurnBuilder::new(MODEL)
            .tool_call("call_1", "big", &json!({}))
            .build(),
        TurnBuilder::new(MODEL)
            .tool_call(
                "call_2",
                "read_file",
                &json!({"path": path, "offset": 2000, "limit": 1}),
            )
            .build(),
        TurnBuilder::new(MODEL).text("done").build(),
    ]));
    let sessions = Arc::new(MemorySessions::new());
    let files: Arc<dyn SessionFiles> = sessions.clone();
    let tools = ToolRegistry::new();
    let schema = json!({"type": "object"}).as_object().unwrap().clone();
    let _big = tools.register(Arc::new(Big { schema })).unwrap();
    let _vfs: Vec<_> = (vfs_tools(files.clone()).into_iter())
        .map(|tool| tools.register(tool).unwrap())
        .collect();
    let hooks = Hooks::default();
    let offload = ResultOffload::new(files, OffloadConfig::new(1_000)).unwrap();
    let _offload = hooks.post_tool_use.register(Arc::new(offload)).unwrap();
    let store = Arc::new(MemorySessionStore::new());
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider.clone())
        .tools(Arc::new(tools))
        .hooks(Arc::new(hooks))
        .permissions(
            PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap(),
        )
        .run_store(Arc::new(MemoryRunStore::new()))
        .session_store(store.clone())
        .namespace("ns")
        .build()
        .unwrap();

    let session = SessionId::from("s1");
    let mut stream = agent.run("go", RunOptions::new(session.clone()));
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.expect("the run succeeds"));
    }

    let marker = "line 1999";
    let pointer = format!("offloaded to '{path}'");
    let events = format!("{events:?}");
    assert!(
        events.contains(&pointer),
        "the tool end carries the pointer"
    );
    assert!(
        !events.contains(marker),
        "the events never carry the payload"
    );
    let log = format!("{:?}", store.load("ns", &session).await.unwrap());
    assert!(log.contains(&pointer), "the session log holds the pointer");
    assert!(
        !log.contains(marker),
        "the session log never holds the payload"
    );
    let requests = provider.requests();
    let second = format!("{:?}", requests[1]);
    assert!(second.contains(&pointer) && !second.contains(marker));

    let stored = sessions.backend(&session).read(path).await.unwrap();
    assert_eq!(stored, payload(), "the backend holds the full result");
    let third = format!("{:?}", requests[2]);
    assert!(
        third.contains("line 2000"),
        "read_file returned the last line"
    );
}
