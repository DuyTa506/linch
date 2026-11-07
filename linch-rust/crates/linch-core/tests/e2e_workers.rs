//! Workers: `SubagentContinue` runs a kept worker again on its own session
//! (its history intact), `TaskStop` stops a background worker, and closing
//! a parent session closes its workers' sessions too.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::subagents::*;
use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, Event, InMemoryMailbox, JsonObject, PermissionMode, RunId, RunOptions,
    RunStatus, RunStore, ScriptedTurn, SessionId, SubagentDefinition, Tool, ToolContext,
    ToolOutput, ToolRegistry, ToolResult, ToolScope, Workers,
};
use serde_json::{Value, json};
use tokio::sync::Notify;

fn call(id: &str, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(id, tool, &input)
        .usage(usage(10, 5))
        .build()
}

fn spawn(id: &str, kind: Option<&str>, prompt: &str, background: bool) -> ScriptedTurn {
    let mut input = json!({"description": "look", "prompt": prompt});
    if let Some(kind) = kind {
        input["subagent_type"] = json!(kind);
    }
    if background {
        input["run_in_background"] = json!(true);
    }
    call(id, "Subagent", input)
}

/// Runs until its call is cancelled.
struct Block(Arc<Notify>, JsonObject);

#[async_trait]
impl Tool for Block {
    fn name(&self) -> &str {
        "Block"
    }
    fn description(&self) -> &str {
        "Block."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.1
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, ctx: ToolContext) -> ToolResult {
        self.0.notify_one();
        ctx.cancel.cancelled().await;
        Ok(ToolOutput::new("stopped"))
    }
}

type Closed = Arc<Mutex<Vec<String>>>;

fn agent(family: &Arc<Family>, stores: &Arc<Stores>, workers: Workers, closed: &Closed) -> Agent {
    let registry = ToolRegistry::new();
    let block = Block(Arc::new(Notify::new()), object(json!({"type": "object"})));
    let _ = registry.register(Arc::new(block)).unwrap();
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    scout.tools = Some(Vec::new());
    let closed = closed.clone();
    AgentBuilder::new(spec())
        .provider(family.clone())
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .subagents(vec![scout])
        .workers(workers)
        .on_session_close(move |session| closed.lock().unwrap().push(session.to_string()))
        .build()
        .unwrap()
}

async fn run_r1(agent: &Agent) -> Vec<linch_core::Envelope> {
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    finish(agent.run("go", options)).await
}

fn retain() -> Workers {
    Workers {
        retain: true,
        ..Workers::default()
    }
}

#[tokio::test]
async fn a_kept_worker_continues_on_its_own_session() {
    let continue_ = json!({"to": "r1:sub_0", "message": "and y?"});
    let parent = vec![
        spawn("sub_0", None, "check x", false),
        call("c_0", "SubagentContinue", continue_),
        text_turn("done"),
    ];
    let child = vec![text_turn("x holds"), text_turn("y too")];
    let family = Arc::new(Family::new(parent, child));
    let stores = Arc::new(Stores::default());
    let closed = Closed::default();
    let agent = agent(&family, &stores, retain(), &closed);
    run_r1(&agent).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[2]), ["x holds\n\n[Worker ID: r1:sub_0]"]);
    assert_eq!(result_texts(&log[4]), ["y too"]);
    let again = stores.runs.load(NS, &RunId::new("r1:c_0")).await.unwrap();
    assert_eq!(again.session_id, SessionId::new("r1:sub_0"));
    assert_eq!(again.parent_run_id, Some(RunId::new("r1")));
    assert_eq!(again.status, RunStatus::Completed);
    let worker_log = messages(&*stores, "r1:sub_0").await;
    let texts: Vec<String> = worker_log.iter().map(|m| m.text()).collect();
    assert_eq!(texts, ["check x", "x holds", "and y?", "y too"]);
    let names: Vec<String> = (family.requests_of("parent")[0].tools.iter())
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(names, ["Block", "Subagent", "SubagentContinue"]);
    let continue_tool = &family.requests_of("parent")[0].tools[2];
    let to = continue_tool.input_schema["properties"]["to"]["description"].to_string();
    assert!(to.contains("'<parent run id>:<tool_use_id>'"), "{to}");
    let child_names: Vec<String> = (family.requests_of("check x")[0].tools.iter())
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(child_names, ["Block"], "a child gets no worker tools");

    assert!(closed.lock().unwrap().is_empty(), "kept workers stay open");
    agent.close_session(&SessionId::new("s1"));
    assert_eq!(*closed.lock().unwrap(), ["r1:sub_0", "s1"]);
}

#[tokio::test]
async fn a_worker_is_found_by_its_name_and_a_wrong_name_is_refused() {
    let parent = vec![
        spawn("sub_0", Some("scout"), "check x", false),
        call(
            "c_0",
            "SubagentContinue",
            json!({"to": "nobody", "message": "hi"}),
        ),
        call(
            "c_1",
            "SubagentContinue",
            json!({"to": "scout", "message": "and y?"}),
        ),
        text_turn("done"),
    ];
    let child = vec![text_turn("x holds"), text_turn("y too")];
    let family = Arc::new(Family::new(parent, child));
    let stores = Arc::new(Stores::default());
    let agent = agent(&family, &stores, retain(), &Closed::default());
    run_r1(&agent).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[4]),
        [
            "No live worker 'nobody'. Known worker ids: r1:sub_0. Spawn a fresh Subagent if this \
          worker is no longer needed."
        ]
    );
    assert_eq!(result_texts(&log[6]), ["y too"]);
}

#[tokio::test]
async fn task_stop_stops_a_background_worker_without_a_notification() {
    let stop = json!({"task_id": "r1:sub_0", "reason": "enough"});
    let parent = vec![
        spawn("sub_0", None, "check x", true),
        call("stop_0", "TaskStop", stop),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, vec![tool_turn(0, 1, "Block")]));
    let stores = Arc::new(Stores::default());
    let workers = Workers {
        background: true,
        ..Workers::default()
    };
    let agent = agent(&family, &stores, workers, &Closed::default());
    let envelopes = run_r1(&agent).await;

    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[4]),
        [
            "Worker 'r1:sub_0' (Agent #1) stopped. Reason: enough. Use SubagentContinue to \
          re-engage if needed."
        ]
    );
    assert_eq!(log.last().unwrap().text(), "done");
    assert!(
        log.iter()
            .all(|m| !m.text().contains("<task-notification>"))
    );
    let tools = &family.requests_of("parent")[0].tools;
    let stop = tools.iter().find(|t| t.name == "TaskStop").unwrap();
    let id = stop.input_schema["properties"]["task_id"]["description"].to_string();
    assert!(id.contains("'<parent run id>:<tool_use_id>'"), "{id}");
    let child = stores.runs.load(NS, &RunId::new("r1:sub_0")).await.unwrap();
    assert_eq!(child.status, RunStatus::Aborted);
    let ends: Vec<RunStatus> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ChildRunStatus(mark) => Some(mark.status),
            _ => None,
        })
        .collect();
    assert_eq!(ends, [RunStatus::Running, RunStatus::Aborted]);
}

#[tokio::test]
async fn a_continue_to_a_running_worker_is_refused_and_leaves_it_running() {
    let continue_ = json!({"to": "r1:sub_0", "message": "more"});
    let stop = json!({"task_id": "r1:sub_0"});
    let parent = vec![
        spawn("sub_0", None, "check x", true),
        call("c_0", "SubagentContinue", continue_),
        call("stop_0", "TaskStop", stop),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, vec![tool_turn(0, 1, "Block")]));
    let stores = Arc::new(Stores::default());
    let workers = Workers {
        background: true,
        ..Workers::default()
    };
    let agent = agent(&family, &stores, workers, &Closed::default());
    let envelopes = run_r1(&agent).await;

    let log = messages(&*stores, "s1").await;
    let refused = result_texts(&log[4]);
    assert!(refused[0].contains("is still running"), "{refused:?}");
    let ends: Vec<RunStatus> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ChildRunStatus(mark) => Some(mark.status),
            _ => None,
        })
        .collect();
    assert_eq!(
        ends,
        [RunStatus::Running, RunStatus::Aborted],
        "the refused continue did not fail the worker"
    );
    let child = stores.runs.load(NS, &RunId::new("r1:sub_0")).await.unwrap();
    assert_eq!(child.status, RunStatus::Aborted);
    let refused = stores.runs.load(NS, &RunId::new("r1:c_0")).await;
    assert!(refused.is_err(), "a refused continue starts no run");
}

#[tokio::test]
async fn task_stop_on_a_finished_worker_does_not_change_its_status() {
    let stop = json!({"task_id": "r1:sub_0"});
    let parent = vec![
        spawn("sub_0", None, "check x", false),
        call("stop_0", "TaskStop", stop),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, vec![text_turn("x holds")]));
    let stores = Arc::new(Stores::default());
    let workers = Workers {
        background: true,
        retain: true,
        ..Workers::default()
    };
    let agent = agent(&family, &stores, workers, &Closed::default());
    let envelopes = run_r1(&agent).await;

    let child = stores.runs.load(NS, &RunId::new("r1:sub_0")).await.unwrap();
    assert_eq!(
        child.status,
        RunStatus::Completed,
        "not killed after the fact"
    );
    let log = messages(&*stores, "s1").await;
    let replied = &result_texts(&log[4])[0];
    assert!(replied.contains("already finished"), "{replied}");
    assert!(replied.contains("completed"), "{replied}");
    assert!(!replied.contains("stopped"), "{replied}");
    let marks = envelopes
        .iter()
        .filter(|e| matches!(e.event, Event::ChildRunStatus(_)));
    assert_eq!(marks.count(), 0, "a foreground worker has no status marks");
}

#[tokio::test]
async fn a_definition_that_names_the_worker_tools_still_does_not_get_them() {
    let parent = vec![
        spawn("sub_0", Some("scout"), "check x", false),
        text_turn("done"),
    ];
    let family = Arc::new(Family::new(parent, vec![text_turn("x holds")]));
    let stores = Arc::new(Stores::default());
    let registry = ToolRegistry::new();
    let block = Block(Arc::new(Notify::new()), object(json!({"type": "object"})));
    let _ = registry.register(Arc::new(block)).unwrap();
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    let names = ["Block", "Subagent", "SubagentContinue", "TaskStop"];
    scout.tools = Some(names.map(String::from).to_vec());
    let agent = AgentBuilder::new(spec())
        .provider(family.clone())
        .tools(Arc::new(registry))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(vec![scout])
        .workers(Workers {
            background: true,
            retain: true,
            ..Workers::default()
        })
        .build()
        .unwrap();
    run_r1(&agent).await;
    let child: Vec<String> = (family.requests_of("check x")[0].tools.iter())
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(child, ["Block"]);
}

#[tokio::test]
async fn an_unkept_child_closes_its_session_when_it_ends() {
    let parent = vec![spawn("sub_0", None, "check x", false), text_turn("done")];
    let family = Arc::new(Family::new(parent, vec![text_turn("x holds")]));
    let stores = Arc::new(Stores::default());
    let closed = Closed::default();
    let agent = agent(&family, &stores, Workers::default(), &closed);
    run_r1(&agent).await;
    assert_eq!(*closed.lock().unwrap(), ["r1:sub_0"]);
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        ["x holds"],
        "no worker id without retain"
    );
}

#[tokio::test]
async fn the_worker_tools_never_run_in_parallel_and_go_with_their_agent() {
    let family = Arc::new(Family::new(Vec::new(), Vec::new()));
    let registry = Arc::new(ToolRegistry::new());
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    scout.tools = Some(Vec::new());
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(family)
        .tools(registry.clone())
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(vec![scout])
        .workers(Workers {
            background: true,
            retain: true,
            ..Workers::default()
        })
        .mailbox(Arc::new(InMemoryMailbox::new()))
        .build()
        .unwrap();
    let names = ["Subagent", "SubagentContinue", "TaskStop", "send_message"];
    let snapshot = registry.snapshot();
    for name in &names[1..] {
        let tool = snapshot.resolve(name).unwrap().tool();
        assert!(!tool.parallel(&JsonObject::new()), "{name}");
    }
    let subagent = snapshot.resolve("Subagent").unwrap().tool();
    let mut input = object(json!({"subagent_type": "scout", "prompt": "p", "description": "d"}));
    assert!(subagent.parallel(&input), "a scout reads only");
    input.insert("run_in_background".into(), json!(true));
    assert!(!subagent.parallel(&input), "a background call never");
    drop(agent);
    let snapshot = registry.snapshot();
    assert!(names.iter().all(|name| snapshot.resolve(name).is_none()));
}
