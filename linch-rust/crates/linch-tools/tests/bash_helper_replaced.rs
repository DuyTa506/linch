//! A helper whose executable the sandboxed command can write is not
//! trusted: the command could replace it with a program that names another
//! process group in the pid line the server reads. Here the helper sits in
//! the workspace (as `.venv/bin/linch-server` does in a project directory);
//! the command replaces it, and the server must not signal the group the
//! replacement names. This file is its own test process because the helper
//! is chosen once per process.

#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::Duration;

use common::{Workspace, call};
use linch_tools::reaper::{ReaperCommand, register};
use linch_tools::{Bash, ExecutionBackend, ExecutionConfig, SandboxPolicy};
use serde_json::json;

#[tokio::test]
async fn a_helper_the_sandbox_can_write_is_not_used() {
    let ws = Workspace::new();
    let root = ws.root();
    fs::create_dir(root.join("bin")).unwrap();
    let helper = root.join("bin/linch-reaper");
    fs::copy(env!("CARGO_BIN_EXE_linch-reaper"), &helper).unwrap();
    register(ReaperCommand {
        program: helper,
        args: Vec::new(),
    })
    .unwrap();
    let config = ExecutionConfig::Sandbox(SandboxPolicy::default());
    let backend = match ExecutionBackend::open(ws.fs.clone(), &config) {
        Ok(backend) => backend,
        Err(error) => {
            eprintln!("skipped: {error}");
            return;
        }
    };
    // A process group this test started, outside the sandbox.
    let mut victim = Command::new("sleep")
        .arg("3117")
        .process_group(0)
        .stdin(Stdio::null())
        .spawn()
        .unwrap();
    let pid = victim.id();
    let tool = Bash::in_backend(&backend);
    let command = format!(
        "printf '#!/bin/sh\\necho {pid} >&0\\nshift\\nexec \"$@\"\\n' > bin/x; \
         chmod +x bin/x; mv bin/x bin/linch-reaper"
    );
    let _ = call(&tool, json!({"command": command})).await;
    let _ = call(&tool, json!({"command": "sleep 30", "timeout_ms": 500})).await;
    tokio::time::sleep(Duration::from_millis(500)).await;
    let alive = victim.try_wait().unwrap().is_none();
    let _ = victim.kill();
    let _ = victim.wait();
    assert!(
        alive,
        "the server killed process group {pid}, which a replaced helper named"
    );
}
