//! A background process does not survive the process that started it: a
//! run resumed after a crash that asks for one gets "no longer exists".

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Event, MemoryRunStore, MemorySessionStore, PermissionEngine,
    PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider, ScriptedTurn, ToolRegistry,
};
use linch_tools::{Bash, BashKill, BashOutput, Processes};
use serde_json::json;

const MODEL: &str = "scripted-model";

/// Whether `pid` is in the process table. Without `/proc` a missing entry
/// would read as gone for any pid, so other systems ask `ps`.
fn exists(pid: u64) -> bool {
    #[cfg(target_os = "linux")]
    {
        Path::new(&format!("/proc/{pid}")).exists()
    }
    #[cfg(not(target_os = "linux"))]
    {
        let ps = std::process::Command::new("ps")
            .args(["-o", "stat=", "-p", &pid.to_string()])
            .output()
            .expect("run ps");
        !String::from_utf8_lossy(&ps.stdout).trim().is_empty()
    }
}

fn registry(root: &Path, processes: &Arc<Processes>) -> Arc<ToolRegistry> {
    let tools = ToolRegistry::new();
    let bash = Bash::new(root).with_processes(Arc::clone(processes));
    let _ = tools.register(Arc::new(bash)).unwrap();
    let _ = tools
        .register(Arc::new(BashOutput::new(Arc::clone(processes))))
        .unwrap();
    let _ = tools
        .register(Arc::new(BashKill::new(Arc::clone(processes))))
        .unwrap();
    Arc::new(tools)
}

#[tokio::test]
async fn a_resumed_run_hears_its_process_no_longer_exists() {
    let dir = tempfile::tempdir().unwrap();
    let runs = Arc::new(MemoryRunStore::default());
    let sessions = Arc::new(MemorySessionStore::default());
    let agent = |turns: Vec<ScriptedTurn>, processes: &Arc<Processes>| {
        AgentBuilder::new(AgentSpec::new(MODEL))
            .provider(Arc::new(ScriptedProvider::new(turns)))
            .tools(registry(dir.path(), processes))
            .permissions(
                PermissionEngine::new(PermissionPolicy::new(PermissionMode::SkipDangerous, "/"))
                    .unwrap(),
            )
            .run_store(runs.clone())
            .session_store(sessions.clone())
            .namespace("t")
            .build()
            .unwrap()
    };

    // The first process starts a background command, then dies before the
    // next model call.
    let first = Arc::new(Processes::new());
    let start = json!({"command": "sleep 30", "run_in_background": true});
    let turns = vec![
        TurnBuilder::new(MODEL)
            .tool_call("c1", "Bash", &start)
            .build(),
        TurnBuilder::new(MODEL).text("unused").build(),
    ];
    let crashed = agent(turns, &first);
    let mut stream = crashed.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let metadata = loop {
        match stream.next().await.unwrap().unwrap().event {
            Event::ToolCallEnd(end) => break end.metadata.expect("the start's metadata"),
            _ => continue,
        }
    };
    let id = metadata["process_id"].as_str().unwrap().to_owned();
    let pid = metadata["pid"].as_u64().unwrap();
    drop(stream);
    drop(crashed);
    drop(first);
    let until = Instant::now() + Duration::from_secs(5);
    while exists(pid) {
        assert!(Instant::now() < until, "the process outlived its agent");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    // The next process resumes the run; the model asks for the output.
    let second = Arc::new(Processes::new());
    let turns = vec![
        TurnBuilder::new(MODEL)
            .tool_call("c2", "BashOutput", &json!({"process_id": id}))
            .build(),
        TurnBuilder::new(MODEL).text("It is gone.").build(),
    ];
    let resumed = agent(turns, &second);
    let mut stream = resumed.resume(run_id, RunOptions::new("s1"));
    let mut ends = Vec::new();
    let mut result = None;
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::ToolCallEnd(end) => ends.push(end),
            Event::Result(end) => result = Some(end),
            _ => {}
        }
    }
    let [end] = &ends[..] else {
        panic!("one tool end: {ends:?}")
    };
    assert!(end.result.is_error);
    assert_eq!(end.error_code.as_deref(), Some("no_such_process"));
    let text = serde_json::to_value(&end.result).unwrap()["content"][0]["text"].clone();
    assert!(
        text.as_str().unwrap().contains("no longer exists"),
        "{text}"
    );
    assert_eq!(result.unwrap().final_text.as_deref(), Some("It is gone."));
}

/// The tool ends of one run of an agent that answers `turns`.
async fn tool_ends(
    root: &Path,
    processes: &Arc<Processes>,
    turns: Vec<ScriptedTurn>,
) -> Vec<Event> {
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::new(ScriptedProvider::new(turns)))
        .tools(registry(root, processes))
        .permissions(
            PermissionEngine::new(PermissionPolicy::new(PermissionMode::SkipDangerous, "/"))
                .unwrap(),
        )
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("t")
        .build()
        .unwrap();
    let mut stream = agent.run("go", RunOptions::new("s1"));
    let mut ends = Vec::new();
    while let Some(item) = stream.next().await {
        let event = item.unwrap().event;
        if matches!(event, Event::ToolCallEnd(_)) {
            ends.push(event);
        }
    }
    ends
}

/// A background command that ended badly is an error result, and its status
/// reaches the end event's `metadata` all the same: a client or hook reads
/// `status` and `exit_code` from it, the model sees only the text.
#[tokio::test]
async fn a_failed_poll_carries_its_status_in_the_tool_end_metadata() {
    let dir = tempfile::tempdir().unwrap();
    let processes = Arc::new(Processes::new());
    let start = json!({"command": "echo out; exit 3", "run_in_background": true});
    let turns = vec![
        TurnBuilder::new(MODEL)
            .tool_call("c1", "Bash", &start)
            .build(),
        TurnBuilder::new(MODEL).text("started").build(),
    ];
    let started = tool_ends(dir.path(), &processes, turns).await;
    let [Event::ToolCallEnd(started)] = &started[..] else {
        panic!("one tool end: {started:?}")
    };
    let id = started.metadata.as_ref().unwrap()["process_id"].clone();

    let poll = json!({"process_id": id, "wait_ms": 5000});
    let turns = vec![
        TurnBuilder::new(MODEL)
            .tool_call("c2", "BashOutput", &poll)
            .build(),
        TurnBuilder::new(MODEL).text("done").build(),
    ];
    let ends = tool_ends(dir.path(), &processes, turns).await;
    let [Event::ToolCallEnd(end)] = &ends[..] else {
        panic!("one tool end: {ends:?}")
    };
    assert!(end.result.is_error);
    assert_eq!(end.error_code.as_deref(), Some("exit_status"));
    let metadata = end.metadata.as_ref().expect("a failed call's status");
    assert_eq!(metadata["status"], "exited");
    assert_eq!(metadata["exit_code"], 3);
    assert_eq!(metadata["process_id"], id);
    let text = serde_json::to_value(&end.result).unwrap()["content"][0]["text"].clone();
    assert_eq!(
        text, "out\n[exit code 3]",
        "the model-visible text is as before"
    );
}
