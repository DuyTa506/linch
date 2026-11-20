//! The pid line a helper writes is checked before the server trusts it: the
//! process must be a child of the helper and lead its own group. A helper
//! that names some other process group gets that group left alone and the
//! command stopped. This file is its own test process because the helper is
//! chosen once per process.

#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use common::{Workspace, call};
use linch_tools::Bash;
use linch_tools::reaper::{ReaperCommand, register};
use serde_json::json;

#[tokio::test]
async fn a_pid_that_is_not_the_helpers_shell_is_never_signalled() {
    let ws = Workspace::new();
    // A process group this test started, which the helper will name.
    let mut victim = Command::new("sleep")
        .arg("3117")
        .process_group(0)
        .stdin(Stdio::null())
        .spawn()
        .unwrap();
    let pid = victim.id();
    // Outside the workspace, so only the pid line is untrustworthy.
    let helper = ws.outside().join("liar");
    let script = format!("#!/bin/sh\necho {pid} >&0\nshift\nexec \"$@\"\n");
    fs::write(&helper, script).unwrap();
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755)).unwrap();
    register(ReaperCommand {
        program: helper,
        args: Vec::new(),
    })
    .unwrap();
    let tool = Bash::new(ws.root());
    let started = Instant::now();
    let _ = call(&tool, json!({"command": "sleep 30", "timeout_ms": 500})).await;
    assert!(started.elapsed() < Duration::from_secs(10), "the call hung");
    tokio::time::sleep(Duration::from_millis(500)).await;
    let alive = victim.try_wait().unwrap().is_none();
    let _ = victim.kill();
    let _ = victim.wait();
    assert!(
        alive,
        "the server killed process group {pid}, which the helper named"
    );
}
