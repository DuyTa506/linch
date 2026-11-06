//! A parent that suspends for approval keeps its background children
//! running: a child inside a provider call or a tool when the parent
//! suspends goes on while the parent waits, nothing of it is sent or run
//! twice, and the parent hears of its end after the approve.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use common::subagents::who;
use common::*;
use futures::StreamExt;
use linch_core::hooks::ApprovalResponse;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, Event, JsonObject, PermissionEngine, PermissionMode, PermissionPolicy,
    Phase, Provider, ProviderError, ProviderRequest, ProviderStream, Role, RunId, RunOptions,
    RunStatus, RunStore, ScriptedTurn, SessionId, Tool, ToolContext, ToolOutput, ToolRegistry,
    ToolResult, ToolScope, Workers,
};
use serde_json::json;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

/// One open-once gate: `entered` tells the test the holder reached it.
#[derive(Default)]
struct Gate {
    entered: Notify,
    open: Notify,
}

impl Gate {
    async fn pass(&self) {
        self.entered.notify_one();
        self.open.notified().await;
    }
}

/// The parent spawns children `a` and `b` in the background, then calls
/// `Write`, which asks. Child `a`'s first model call waits at `a_call`;
/// child `b` calls the `Hold` tool, which waits at `b_tool`.
struct Script {
    a_call: Gate,
    calls: Mutex<HashMap<(String, usize), u32>>,
}

fn spawn_both() -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL);
    for (i, prompt) in ["a", "b"].iter().enumerate() {
        let input = json!({"description": "look", "prompt": prompt, "run_in_background": true});
        builder = builder.tool_call(&format!("sub_{i}"), "Subagent", &input);
    }
    builder.usage(usage(10, 5)).build()
}

fn turn(who: &str, n: usize) -> ScriptedTurn {
    match (who, n) {
        ("parent", 0) => spawn_both(),
        ("parent", 1) => tool_turn(1, 1, "Write"),
        ("parent", 2) => text_turn("waiting"),
        ("parent", _) => text_turn("done"),
        ("a", 0) => text_turn("a holds"),
        ("b", 0) => tool_turn(0, 1, "Hold"),
        ("b", _) => text_turn("b holds"),
        other => panic!("unscripted call {other:?}"),
    }
}

#[async_trait]
impl Provider for Script {
    fn id(&self) -> &str {
        "script"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let n = (request.messages.iter())
            .filter(|m| m.role == Role::Assistant)
            .count();
        let who = who(&request);
        *self
            .calls
            .lock()
            .unwrap()
            .entry((who.clone(), n))
            .or_default() += 1;
        if (who.as_str(), n) == ("a", 0) {
            self.a_call.pass().await;
        }
        let events: Vec<_> = turn(&who, n).events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

/// Waits at its gate, then answers `real`; counts its runs.
struct Hold {
    gate: Arc<Gate>,
    runs: Arc<Mutex<u32>>,
    schema: JsonObject,
}

#[async_trait]
impl Tool for Hold {
    fn name(&self) -> &str {
        "Hold"
    }
    fn description(&self) -> &str {
        "Hold."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        *self.runs.lock().unwrap() += 1;
        self.gate.pass().await;
        Ok(ToolOutput::new("real"))
    }
}

struct Fixture {
    provider: Arc<Script>,
    b_tool: Arc<Gate>,
    hold_runs: Arc<Mutex<u32>>,
    write_runs: Runs,
    stores: Arc<Stores>,
    agent: Agent,
}

impl Fixture {
    fn new() -> Self {
        let provider = Arc::new(Script {
            a_call: Gate::default(),
            calls: Mutex::default(),
        });
        let b_tool = Arc::new(Gate::default());
        let hold_runs = Arc::new(Mutex::new(0));
        let write_runs = Runs::default();
        let stores = Arc::new(Stores::default());
        let registry = ToolRegistry::new();
        let hold = Hold {
            gate: b_tool.clone(),
            runs: hold_runs.clone(),
            schema: object(json!({"type": "object"})),
        };
        let _ = registry.register(Arc::new(hold)).unwrap();
        let write = Echo::new("Write", ToolScope::Write, &write_runs);
        let _ = registry.register(Arc::new(write)).unwrap();
        let mut policy = PermissionPolicy::new(PermissionMode::Default, "/");
        policy.auto_allow.insert("Subagent".to_owned());
        let workers = Workers {
            background: true,
            ..Workers::default()
        };
        let agent = AgentBuilder::new(spec())
            .provider(provider.clone())
            .tools(Arc::new(registry))
            .permissions(PermissionEngine::new(policy).unwrap())
            .run_store(stores.clone())
            .session_store(stores.clone())
            .namespace(NS)
            .subagents(Vec::new())
            .workers(workers)
            .build()
            .unwrap();
        Self {
            provider,
            b_tool,
            hold_runs,
            write_runs,
            stores,
            agent,
        }
    }

    /// Run the parent until it suspends on the `Write` ask, with child `a`
    /// inside its model call and child `b` inside its tool.
    async fn suspend(&self) -> RunId {
        let mut options = RunOptions::new("s1");
        options.run_id = Some(RunId::new("r1"));
        let envelopes = finish(self.agent.run("go", options)).await;
        let last = &envelopes.last().unwrap().event;
        assert!(matches!(last, Event::Suspended(_)), "{last:?}");
        let run_id = RunId::new("r1");
        let record = self.stores.runs.load(NS, &run_id).await.unwrap();
        assert!(matches!(
            record.checkpoint.phase,
            Phase::AwaitingApproval { .. }
        ));
        let within = Duration::from_secs(5);
        let a = tokio::time::timeout(within, self.provider.a_call.entered.notified()).await;
        a.expect("child a reached its model call");
        let b = tokio::time::timeout(within, self.b_tool.entered.notified()).await;
        b.expect("child b reached its tool");
        run_id
    }

    async fn status(&self, run_id: &str) -> RunStatus {
        let record = self.stores.runs.load(NS, &RunId::new(run_id)).await;
        record.unwrap().status
    }

    /// Whether `child` ends within two seconds.
    async fn ends(&self, child: &str) -> bool {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while tokio::time::Instant::now() < deadline {
            if self.status(child).await.is_terminal() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        false
    }

    fn approve(&self) -> linch_core::RunStream {
        let allow = HashMap::from([("call_1_0".to_owned(), ApprovalResponse::Allow)]);
        let run_id = RunId::new("r1");
        self.agent.approve(run_id, allow, RunOptions::new("s1"))
    }

    /// The children ran once each, the tool result is the real one, and
    /// the parent heard of both ends.
    async fn ran_once(&self) {
        let calls = self.provider.calls.lock().unwrap().clone();
        assert_eq!(calls.get(&("a".into(), 0)), Some(&1), "{calls:?}");
        assert_eq!(calls.get(&("b".into(), 0)), Some(&1), "{calls:?}");
        assert_eq!(calls.get(&("b".into(), 1)), Some(&1), "{calls:?}");
        assert_eq!(*self.hold_runs.lock().unwrap(), 1);
        assert_eq!(self.write_runs.lock().unwrap().get("call_1_0"), Some(&1));
        let b_log = messages(&*self.stores, "r1:sub_1").await;
        assert_eq!(result_texts(&b_log[2]), ["real"]);
        assert_ne!(result_texts(&b_log[2]), [INTERRUPTED_TOOL_TEXT]);
        for child in ["r1:sub_0", "r1:sub_1"] {
            assert_eq!(self.status(child).await, RunStatus::Completed, "{child}");
        }
        assert_eq!(self.status("r1").await, RunStatus::Completed);
        let log = messages(&*self.stores, "s1").await;
        let all: String = log.iter().map(|m| m.text()).collect();
        for child in ["r1:sub_0", "r1:sub_1"] {
            let note = format!("<task-id>{child}</task-id><status>completed</status>");
            assert_eq!(all.matches(&note).count(), 1, "{child}: {all}");
        }
        assert!(all.contains("<result>a holds</result>"), "{all}");
        assert!(all.contains("<result>b holds</result>"), "{all}");
    }
}

#[tokio::test]
async fn a_suspended_parent_keeps_its_background_children_running() {
    let fixture = Fixture::new();
    let run_id = fixture.suspend().await;
    // Both children are mid-flight; they finish while the parent waits.
    fixture.provider.a_call.open.notify_one();
    fixture.b_tool.open.notify_one();
    let a = fixture.ends("r1:sub_0").await;
    let b = fixture.ends("r1:sub_1").await;
    assert_eq!(
        fixture.status(run_id.as_str()).await,
        RunStatus::AwaitingApproval
    );

    finish(fixture.approve()).await;
    fixture.ran_once().await;
    assert!(
        a && b,
        "the children ran on while the parent waited: {a} {b}"
    );
}

#[tokio::test]
async fn an_approve_takes_over_a_child_that_still_runs() {
    let fixture = Fixture::new();
    fixture.suspend().await;
    fixture.provider.a_call.open.notify_one();
    assert!(fixture.ends("r1:sub_0").await, "child a ran on");

    // Child b is still inside its tool when the approve's stream starts.
    let mut stream = fixture.approve();
    let first = stream.next().await;
    assert!(matches!(first, Some(Ok(_))), "{first:?}");
    fixture.b_tool.open.notify_one();
    let rest = tokio::time::timeout(Duration::from_secs(5), drain(stream)).await;
    let (_, error) = rest.expect("the approved run drove child b to its end");
    assert!(error.is_none(), "{error:?}");
    fixture.ran_once().await;
}

#[tokio::test]
async fn closing_the_session_of_a_suspended_parent_aborts_its_children() {
    let fixture = Fixture::new();
    fixture.suspend().await;
    fixture.agent.close_session(&SessionId::new("s1"));
    for child in ["r1:sub_0", "r1:sub_1"] {
        assert!(fixture.ends(child).await, "{child} ended");
        assert_eq!(fixture.status(child).await, RunStatus::Aborted, "{child}");
    }

    // The approve hears both aborts, once each.
    finish(fixture.approve()).await;
    assert_eq!(fixture.status("r1").await, RunStatus::Completed);
    let log = messages(&*fixture.stores, "s1").await;
    let all: String = log.iter().map(|m| m.text()).collect();
    for child in ["r1:sub_0", "r1:sub_1"] {
        let note = format!("<task-id>{child}</task-id><status>aborted</status>");
        assert_eq!(all.matches(&note).count(), 1, "{child}: {all}");
    }
    let calls = fixture.provider.calls.lock().unwrap().clone();
    assert_eq!(calls.get(&("a".into(), 0)), Some(&1), "{calls:?}");
    assert_eq!(*fixture.hold_runs.lock().unwrap(), 1);
}

#[tokio::test]
async fn dropping_the_agent_of_a_suspended_parent_aborts_its_children() {
    let fixture = Fixture::new();
    fixture.suspend().await;
    let Fixture {
        agent,
        stores,
        provider,
        ..
    } = fixture;
    drop(agent);
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    for child in ["r1:sub_0", "r1:sub_1"] {
        loop {
            let status = stores
                .runs
                .load(NS, &RunId::new(child))
                .await
                .unwrap()
                .status;
            if status.is_terminal() {
                assert_eq!(status, RunStatus::Aborted, "{child}");
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "{child} never ended"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
    let calls = provider.calls.lock().unwrap().clone();
    assert_eq!(calls.get(&("a".into(), 0)), Some(&1), "{calls:?}");
}
