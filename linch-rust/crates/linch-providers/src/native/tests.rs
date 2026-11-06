//! A session's catalog changes through a compaction, rendered natively by
//! both adapters: the requests of a real scripted run, fed to each
//! payload builder.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, CompactionSpec, JsonObject, JsonValue, MemoryRunStore,
    MemorySessionStore, PermissionEngine, PermissionMode, PermissionPolicy, ProviderRequest,
    Registration, RunOptions, ScriptedProvider, ScriptedTurn, Tool, ToolChoice, ToolContext,
    ToolOutput, ToolRegistry, ToolResult, ToolScope, Usage,
};
use serde_json::json;

use crate::ApiKey;
use crate::anthropic::payload as anthropic;
use crate::responses::{ResponsesOptions, payload as responses};

const MODEL: &str = "claude-opus-5-5";

fn schema() -> JsonObject {
    let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
    schema.as_object().unwrap().clone()
}

/// A read tool that answers its name.
struct Plain {
    name: &'static str,
    description: &'static str,
    schema: JsonObject,
}

impl Plain {
    fn new(name: &'static str, description: &'static str) -> Arc<Self> {
        let schema = schema();
        Arc::new(Self {
            name,
            description,
            schema,
        })
    }
}

#[async_trait]
impl Tool for Plain {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        self.description
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        Ok(ToolOutput::new(self.name))
    }
}

/// The registration of `Late`, while it lasts.
type Slot = Arc<Mutex<Option<Registration>>>;

/// A write tool that changes `Late`'s registration when it runs.
struct Change {
    name: &'static str,
    schema: JsonObject,
    registry: Arc<ToolRegistry>,
    late: Slot,
    action: fn(&ToolRegistry, &mut Option<Registration>),
}

#[async_trait]
impl Tool for Change {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Change the catalog."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        (self.action)(&self.registry, &mut self.late.lock().unwrap());
        Ok(ToolOutput::new("changed"))
    }
}

fn add_late(registry: &ToolRegistry, late: &mut Option<Registration>) {
    *late = Some(registry.register(Plain::new("Late", "plain tool")).unwrap());
}

fn drop_late(_: &ToolRegistry, late: &mut Option<Registration>) {
    late.take().unwrap().dispose();
}

fn redescribe(registry: &ToolRegistry, late: &mut Option<Registration>) {
    let tool = Plain::new("Late", "a louder plain tool");
    *late = Some(registry.replace(tool).unwrap());
}

fn call(turn: usize, tool: &str) -> ScriptedTurn {
    let usage = Usage {
        input_tokens: 10,
        output_tokens: 5,
        ..Usage::default()
    };
    (TurnBuilder::new(MODEL).text("working"))
        .tool_call(&format!("call_{turn}"), tool, &json!({"value": "v"}))
        .usage(usage)
        .build()
}

fn text(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL).text(text).build()
}

/// The requests of one run whose turn 0 adds `Late` and whose turn 1
/// calls `second`, which runs `action`; a compaction then runs before
/// the answer. Requests: turn 0, turn 1, the summary, the answer.
async fn requests(
    second: &'static str,
    action: fn(&ToolRegistry, &mut Option<Registration>),
) -> Vec<ProviderRequest> {
    let registry = Arc::new(ToolRegistry::new());
    let late: Slot = Arc::default();
    let _ = registry.register(Plain::new("Echo", "plain tool")).unwrap();
    for (name, action) in [("AddLate", add_late as fn(&_, &mut _)), (second, action)] {
        let change = Change {
            name,
            schema: schema(),
            registry: Arc::clone(&registry),
            late: Arc::clone(&late),
            action,
        };
        let _ = registry.register(Arc::new(change)).unwrap();
    }
    let turns = [call(0, "AddLate"), call(1, second), text("summary")];
    let provider = Arc::new(ScriptedProvider::new(
        turns.into_iter().chain([text("done")]),
    ));
    let mut spec = AgentSpec::new(MODEL);
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .tools(registry)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::new()))
        .session_store(Arc::new(MemorySessionStore::new()))
        .namespace("ns")
        .build()
        .unwrap();
    let mut stream = agent.run("go", RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    let requests = provider.requests();
    assert_eq!(requests.len(), 4, "turn 0, turn 1, the summary, the answer");
    let summary = &requests[2].tool_choice;
    assert_eq!(summary, &Some(ToolChoice::Disabled {}), "the summary call");
    requests
}

/// Each request's body from both adapters, Anthropic's then Responses',
/// with catalog changes rendered natively or not.
fn bodies(requests: &[ProviderRequest], native: bool) -> [Vec<JsonValue>; 2] {
    let options = ResponsesOptions {
        native_catalog_changes: native,
        ..ResponsesOptions::new(ApiKey::new("sk-unit-test-native-1c2d").unwrap())
    };
    let anthropic = (requests.iter())
        .map(|request| anthropic::build(request, native).unwrap())
        .collect();
    let responses = (requests.iter())
        .map(|request| {
            let request = ProviderRequest {
                model: "gpt-5.4".into(),
                ..request.clone()
            };
            responses::build_payload(&request, &options).unwrap()
        })
        .collect();
    [anthropic, responses]
}

/// The names of each request's `tools`, as both adapters send them with
/// catalog changes rendered natively.
fn native_tools(requests: &[ProviderRequest]) -> [Vec<Vec<String>>; 2] {
    let names = |body: &JsonValue| -> Vec<String> {
        let tools = body["tools"].as_array().cloned().unwrap_or_default();
        let name = |tool: &JsonValue| tool["name"].as_str().unwrap().to_owned();
        tools.iter().map(name).collect()
    };
    bodies(requests, true).map(|bodies| bodies.iter().map(names).collect())
}

#[tokio::test]
async fn a_late_tool_removed_before_a_compaction_stays_out_of_tools() {
    let requests = requests("DropLate", drop_late).await;
    for (adapter, tools) in ["anthropic", "responses"]
        .iter()
        .zip(native_tools(&requests))
    {
        for (n, names) in tools.iter().enumerate() {
            assert_eq!(names, &["Echo", "AddLate", "DropLate"], "{adapter} {n}");
        }
    }
}

#[tokio::test]
async fn a_late_tool_with_a_new_description_stays_out_of_tools_after_a_compaction() {
    let requests = requests("Redescribe", redescribe).await;
    for (adapter, tools) in ["anthropic", "responses"]
        .iter()
        .zip(native_tools(&requests))
    {
        for (n, names) in tools.iter().enumerate() {
            assert_eq!(names, &["Echo", "AddLate", "Redescribe"], "{adapter} {n}");
        }
    }
}

#[tokio::test]
async fn with_native_changes_off_the_marks_change_no_byte() {
    for (second, action) in [
        ("DropLate", drop_late as fn(&_, &mut _)),
        ("Redescribe", redescribe),
    ] {
        let requests = requests(second, action).await;
        let unmarked: Vec<ProviderRequest> = (requests.iter())
            .map(|request| ProviderRequest {
                appended_tools: Vec::new(),
                ..request.clone()
            })
            .collect();
        assert!(requests[1].appended_tools == ["Late"], "{second}");
        let bytes =
            |bodies: [Vec<JsonValue>; 2]| bodies.map(|b| serde_json::to_string(&b).unwrap());
        assert_eq!(
            bytes(bodies(&requests, false)),
            bytes(bodies(&unmarked, false)),
            "{second}"
        );
    }
}
