//! A workspace agent's background `Bash` commands end with their session
//! (`Agent::close_session`) and with the agent.

use std::path::Path;
use std::time::{Duration, Instant};

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, OpenedAgent, RunOptions, SessionId};
use serde_json::json;

const MODEL: &str = "scripted-model";

async fn open(root: &Path) -> OpenedAgent {
    let start = json!({"command": "sleep 30", "run_in_background": true});
    let turns = [
        TurnBuilder::new(MODEL)
            .tool_call("c1", "Bash", &start)
            .build(),
        TurnBuilder::new(MODEL).text("started").build(),
    ];
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "workspace": root,
        "permissions": {"mode": "skip_dangerous", "project_root": root},
    }))
    .unwrap();
    config.open().await.unwrap()
}

/// Run session `s1` to its end; the pid of the command it started.
async fn started(opened: &OpenedAgent) -> u64 {
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let mut pid = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            pid = end.metadata.and_then(|m| m["pid"].as_u64());
        }
    }
    pid.expect("Bash started a background command")
}

async fn reaped(pid: u64) {
    let until = Instant::now() + Duration::from_secs(5);
    while Path::new(&format!("/proc/{pid}")).exists() {
        assert!(Instant::now() < until, "process {pid} was not reaped");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn closing_the_session_ends_its_background_commands() {
    let dir = tempfile::tempdir().unwrap();
    let opened = open(dir.path()).await;
    let pid = started(&opened).await;
    assert!(
        Path::new(&format!("/proc/{pid}")).exists(),
        "it outlives its run"
    );
    opened.agent().close_session(&SessionId::new("s1"));
    reaped(pid).await;
}

#[tokio::test]
async fn closing_the_agent_ends_its_background_commands() {
    let dir = tempfile::tempdir().unwrap();
    let opened = open(dir.path()).await;
    let pid = started(&opened).await;
    opened.close().await;
    reaped(pid).await;
}
