//! The `docker` execution backend against a real daemon. Every test skips
//! (with a note on stderr) when `docker info` fails, as it does for a user
//! without access to the daemon.

mod common;

use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use common::{Workspace, ctx, input, text};
use linch_core::{SessionId, Tool, ToolResult};
use linch_tools::{Bash, BashOutput, DockerPolicy, ExecutionBackend, ExecutionConfig, Processes};
use serde_json::json;

/// The image the tests run: small, with a shell.
const IMAGE: &str = "busybox:1.36";

fn docker() -> bool {
    let ok = Command::new("docker")
        .arg("info")
        .output()
        .is_ok_and(|out| out.status.success());
    if !ok {
        eprintln!("skipped: docker info failed (no Docker daemon reachable)");
    }
    ok
}

fn backend(ws: &Workspace) -> ExecutionBackend {
    let policy = DockerPolicy {
        image: IMAGE.into(),
        ..DockerPolicy::default()
    };
    ExecutionBackend::open(ws.fs.clone(), &ExecutionConfig::Docker(policy)).unwrap()
}

async fn run(tool: &Bash, command: &str) -> ToolResult {
    let value = json!({"command": command, "timeout_ms": 120_000});
    tool.execute(input(value), ctx("s1")).await
}

/// Names of the containers that are named like this crate's.
fn containers() -> Vec<String> {
    let out = Command::new("docker")
        .args([
            "ps",
            "-a",
            "--filter",
            "name=linch-",
            "--format",
            "{{.Names}}",
        ])
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::to_owned)
        .collect()
}

#[tokio::test]
async fn a_command_runs_in_a_container_on_the_workspace() {
    if !docker() {
        return;
    }
    let ws = Workspace::new();
    let tool = Bash::in_backend(&backend(&ws));
    // `cat` adds no newline, so the file ends with one before `id -u`'s line.
    ws.put("in.txt", "from the host\n");
    let out = text(run(&tool, "cat in.txt; echo back > out.txt; id -u").await);
    let uid = rustix::process::getuid().as_raw();
    assert_eq!(out, format!("from the host\n{uid}\n[exit code 0]"));
    assert_eq!(ws.get("out.txt"), "back\n");
    let env = text(run(&tool, "env").await);
    assert!(!env.contains("CARGO"), "{env}");
    let net = run(&tool, "wget -q -T 3 -O- http://1.1.1.1").await;
    assert!(net.is_err(), "the network is off");
}

#[tokio::test]
async fn a_timed_out_or_killed_command_leaves_no_container() {
    if !docker() {
        return;
    }
    let ws = Workspace::new();
    let backend = backend(&ws);
    let before = containers();
    let tool = Bash::in_backend(&backend);
    let value = json!({"command": "sleep 60", "timeout_ms": 3_000});
    let timed_out = tool.execute(input(value), ctx("s1")).await.unwrap_err();
    assert_eq!(timed_out.code.as_deref(), Some("timeout"));
    let processes = Arc::new(Processes::new());
    let tool = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    let value = json!({"command": "echo up; sleep 60", "run_in_background": true});
    let started = tool.execute(input(value), ctx("s1")).await.unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    let output = BashOutput::new(Arc::clone(&processes));
    let read = json!({"process_id": id, "wait_ms": 500});
    let _ = text(output.execute(input(read), ctx("s1")).await);
    processes.close_session(&SessionId::from("s1"));
    for _ in 0..100 {
        if containers() == before {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(containers(), before);
}
