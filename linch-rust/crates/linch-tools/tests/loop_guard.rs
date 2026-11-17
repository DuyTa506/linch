//! The loop guard on the built-in tools, on by default: polling a running
//! background command and reading a file again after an edit are not
//! loops; the same read of an unchanged file, or the same `ListSchedules`
//! with the same answer, three times is.

mod common;

use std::sync::Arc;

use async_trait::async_trait;
use common::Workspace;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Event, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent, ProviderRequest,
    ProviderStream, Role, RunOptions, ScriptedTurn, SessionId, Tool, ToolOutputError, ToolRegistry,
};
use linch_tools::{
    Bash, BashKill, BashOutput, Edit, NewSchedule, Processes, Read, ScheduleBackend, ScheduleEntry,
    schedule_tools,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

const MODEL: &str = "scripted-model";

/// A provider that answers each request with `answer(request, n)`, `n`
/// the number of assistant messages the request holds.
struct Answering(Box<Answer>);

type Answer = dyn Fn(&ProviderRequest, usize) -> ScriptedTurn + Send + Sync;

#[async_trait]
impl Provider for Answering {
    fn id(&self) -> &str {
        "answering"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let n = (request.messages.iter())
            .filter(|message| message.role == Role::Assistant)
            .count();
        let turn = (self.0)(&request, n);
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            turn.events.into_iter().map(Ok).collect();
        let stream: BoxStream<'static, _> = futures::stream::iter(events).boxed();
        Ok(stream)
    }
}

fn json_str(value: &impl serde::Serialize) -> String {
    serde_json::to_value(value)
        .unwrap()
        .as_str()
        .unwrap()
        .to_owned()
}

fn call(n: usize, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(&format!("c{n}"), tool, &input)
        .build()
}

fn text(answer: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL).text(answer).build()
}

/// The scripted turns in order, then `done`.
fn script(turns: Vec<ScriptedTurn>) -> Answering {
    Answering(Box::new(move |_, n| {
        turns.get(n).cloned().unwrap_or_else(|| text("done"))
    }))
}

fn agent(provider: Answering, tools: Vec<Arc<dyn Tool>>) -> Agent {
    let registry = ToolRegistry::new();
    for tool in tools {
        let _ = registry.register(tool).unwrap();
    }
    let policy = PermissionPolicy::new(PermissionMode::SkipDangerous, "/");
    AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::new(provider))
        .tools(Arc::new(registry))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("t")
        .build()
        .unwrap()
}

/// The run's result subtype, its tool ends by tool, and its `loop_guard`
/// reasons.
async fn run(agent: Agent) -> (String, Vec<String>, Vec<String>) {
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let (mut ends, mut guards, mut subtype) = (Vec::new(), Vec::new(), String::new());
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::ToolCallEnd(end) => ends.push(end.tool_name),
            Event::LoopGuard(guard) => guards.push(json_str(&guard.reason)),
            Event::Result(result) => subtype = json_str(&result.subtype),
            _ => {}
        }
    }
    (subtype, ends, guards)
}

/// `BashOutput` is polled while a background command runs: ten reads of
/// the same still-running command with the same answer are waiting, not a
/// loop.
#[tokio::test]
async fn polling_a_running_command_is_not_a_loop() {
    let dir = tempfile::tempdir().unwrap();
    let processes = Arc::new(Processes::new());
    let provider = Answering(Box::new(|request, n| {
        let id = || {
            let all = serde_json::to_string(&request.messages).unwrap();
            let at = all.find("bash_").expect("the background command's id");
            all[at..at + 17].to_owned()
        };
        match n {
            0 => call(
                n,
                "Bash",
                json!({"command": "sleep 30", "run_in_background": true}),
            ),
            1..=10 => call(n, "BashOutput", json!({"process_id": id()})),
            11 => call(n, "BashKill", json!({"process_id": id()})),
            _ => text("done"),
        }
    }));
    let bash = Bash::new(dir.path()).with_processes(Arc::clone(&processes));
    let tools: Vec<Arc<dyn Tool>> = vec![
        Arc::new(bash),
        Arc::new(BashOutput::new(Arc::clone(&processes))),
        Arc::new(BashKill::new(Arc::clone(&processes))),
    ];
    let (status, ends, guards) = run(agent(provider, tools)).await;
    assert_eq!(status, "success", "{guards:?}");
    let polls = ends.iter().filter(|name| *name == "BashOutput").count();
    assert_eq!(polls, 10);
}

/// The tools that poll a background command: `BashOutput` reads it, `Bash`
/// starts it.
fn background_tools(dir: &std::path::Path) -> Vec<Arc<dyn Tool>> {
    let processes = Arc::new(Processes::new());
    let bash = Bash::new(dir).with_processes(Arc::clone(&processes));
    vec![
        Arc::new(bash),
        Arc::new(BashOutput::new(Arc::clone(&processes))),
        Arc::new(BashKill::new(processes)),
    ]
}

/// The id of the background command in the messages of `request`.
fn process_id(request: &ProviderRequest) -> String {
    let all = serde_json::to_string(&request.messages).unwrap();
    let at = all.find("bash_").expect("the background command's id");
    all[at..at + 17].to_owned()
}

/// A command that finished answers the same `[exit code 0]` to every read:
/// that is no wait, so the third identical read ends the run, where it used
/// to loop until the budget did.
#[tokio::test]
async fn reading_a_finished_command_again_is_a_loop() {
    let dir = tempfile::tempdir().unwrap();
    let provider = Answering(Box::new(|request, n| match n {
        0 => call(
            n,
            "Bash",
            json!({"command": "echo hi", "run_in_background": true}),
        ),
        1..=12 => call(
            n,
            "BashOutput",
            json!({"process_id": process_id(request), "wait_ms": 5000}),
        ),
        _ => text("done"),
    }));
    let (status, ends, guards) = run(agent(provider, background_tools(dir.path()))).await;
    assert_eq!(status, "error");
    assert_eq!(guards, ["repeated_tool_call"]);
    // The first read has the output; the next three say only the exit code.
    let polls = ends.iter().filter(|name| *name == "BashOutput").count();
    assert_eq!(polls, 4);
}

/// A command that runs on and prints nothing answers the same "still
/// running" for as long as it is polled: it is waited for, up to the guard's
/// `max_idle_turns` identical answers in a row, and then the run ends.
#[tokio::test]
async fn polling_a_silent_running_command_forever_is_a_loop() {
    let dir = tempfile::tempdir().unwrap();
    let provider = Answering(Box::new(|request, n| match n {
        0 => call(
            n,
            "Bash",
            json!({"command": "sleep 30", "run_in_background": true}),
        ),
        1..=60 => call(n, "BashOutput", json!({"process_id": process_id(request)})),
        _ => text("done"),
    }));
    let (status, ends, guards) = run(agent(provider, background_tools(dir.path()))).await;
    assert_eq!(status, "error");
    assert_eq!(guards, ["repeated_tool_call"]);
    let polls = ends.iter().filter(|name| *name == "BashOutput").count();
    assert_eq!(
        polls, 26,
        "25 identical answers are allowed, the 26th ends it"
    );
}

/// Reading a file again after an edit returns new content: progress. The
/// same read of an unchanged file three times is a loop.
#[tokio::test]
async fn a_read_after_an_edit_is_progress_and_an_unchanged_read_is_not() {
    let workspace = Workspace::new();
    workspace.put("a.txt", "one\n");
    let path = workspace.root().join("a.txt").display().to_string();
    let read = || json!({"file_path": path});
    let edit = json!({"file_path": path, "old_string": "one", "new_string": "two"});
    let tools = || -> Vec<Arc<dyn Tool>> {
        let (fs, reads) = (workspace.fs.clone(), workspace.reads.clone());
        vec![
            Arc::new(Read::new(fs.clone(), reads.clone())),
            Arc::new(Edit::new(fs, reads)),
        ]
    };

    let turns = vec![
        call(0, "Read", read()),
        call(1, "Edit", edit),
        call(2, "Read", read()),
    ];
    let (status, ends, guards) = run(agent(script(turns), tools())).await;
    assert_eq!(status, "success", "{guards:?}");
    assert_eq!(ends, ["Read", "Edit", "Read"]);
    assert_eq!(workspace.get("a.txt"), "two\n");

    let turns = (0..5).map(|n| call(n, "Read", read())).collect();
    let (status, ends, guards) = run(agent(script(turns), tools())).await;
    assert_eq!(status, "error");
    assert_eq!(ends, ["Read"; 3]);
    assert_eq!(guards, ["repeated_tool_call"]);
}

/// A fix-and-test loop whose test prints the same failure after every patch:
/// each patch is a different edit, so the identical results are no repeat;
/// the same runs with no patch between are, at the third.
#[tokio::test]
async fn identical_test_results_between_patches_are_not_a_loop() {
    let workspace = Workspace::new();
    workspace.put("a.txt", "v0\n");
    let path = workspace.root().join("a.txt").display().to_string();
    let tools = || -> Vec<Arc<dyn Tool>> {
        let (fs, reads) = (workspace.fs.clone(), workspace.reads.clone());
        vec![
            Arc::new(Read::new(fs.clone(), reads.clone())),
            Arc::new(Edit::new(fs, reads)),
            Arc::new(Bash::new(workspace.root())),
        ]
    };
    let test = json!({"command": "echo '3 tests failing'"});

    let mut turns = vec![call(0, "Read", json!({"file_path": path}))];
    for n in 0..10 {
        let (old, new) = (format!("v{n}"), format!("v{}", n + 1));
        let patch = json!({"file_path": path, "old_string": old, "new_string": new});
        turns.push(call(2 * n + 1, "Edit", patch));
        turns.push(call(2 * n + 2, "Bash", test.clone()));
    }
    let (status, ends, guards) = run(agent(script(turns), tools())).await;
    assert_eq!(status, "success", "{guards:?}");
    assert_eq!(ends.iter().filter(|name| *name == "Bash").count(), 10);
    assert!(guards.is_empty(), "{guards:?}");

    let turns = (0..10).map(|n| call(n, "Bash", test.clone())).collect();
    let (status, ends, guards) = run(agent(script(turns), tools())).await;
    assert_eq!(status, "error");
    assert_eq!(ends, ["Bash"; 3]);
    assert_eq!(guards, ["repeated_tool_call"]);
}

/// Schedules that never change.
struct Fixed;

#[async_trait]
impl ScheduleBackend for Fixed {
    async fn create(&self, _: NewSchedule) -> Result<ScheduleEntry, ToolOutputError> {
        Err(ToolOutputError::new("not here"))
    }

    async fn list(&self, _: &SessionId) -> Result<Vec<ScheduleEntry>, ToolOutputError> {
        Ok(vec![ScheduleEntry {
            id: "a".into(),
            prompt: "digest".into(),
            paused: false,
            next_fire: "2026-11-17T07:00:00.000Z".into(),
        }])
    }

    async fn cancel(&self, _: &SessionId, _: &str) -> Result<bool, ToolOutputError> {
        Ok(false)
    }
}

/// The same `ListSchedules` with the same answer three times is a loop
/// with no progress.
#[tokio::test]
async fn the_same_schedule_listing_three_times_is_a_loop() {
    let turns = (0..5)
        .map(|n| call(n, "ListSchedules", json!({})))
        .collect();
    let (status, ends, guards) = run(agent(script(turns), schedule_tools(Arc::new(Fixed)))).await;
    assert_eq!(status, "error");
    assert_eq!(ends, ["ListSchedules"; 3]);
    assert_eq!(guards, ["repeated_tool_call"]);
}
