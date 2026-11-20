//! By default a local command runs directly under the shell, with no
//! helper process between it and the server: its parent is this process.
//! (`bash_orphans.rs` turns the helper on and tests what it holds.)

#![cfg(target_os = "linux")]

mod common;

use common::{Workspace, call, text};
use linch_tools::Bash;
use serde_json::json;

#[tokio::test]
async fn a_local_command_has_no_helper_unless_one_is_turned_on() {
    let ws = Workspace::new();
    let tool = Bash::new(ws.root());
    let out = text(call(&tool, json!({"command": "echo $PPID"})).await);
    assert_eq!(
        out.lines().next(),
        Some(std::process::id().to_string().as_str())
    );
}
