//! The default local shell runs a command exactly as `Bash` did before
//! execution backends: `/bin/sh -c` in the root, stdin closed, the nine
//! allowlisted host variables and nothing else, its own process group; and
//! a background command under the lifeline wrapper.

mod common;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use common::{Workspace, call, ctx, text};
use linch_core::Tool;
use linch_tools::{Bash, BashOutput, Processes};
use serde_json::json;

/// `$0`, the argument count, the working directory, whether stdin is at
/// end of file, the process group (the shell's own pid) and the sorted
/// environment.
const PROBE: &str = "echo \"0=$0 #=$#\"; pwd; \
                     if read -r _; then echo stdin-open; else echo stdin-eof; fi; \
                     [ \"$(ps -o pgid= -p $$ | tr -d ' ')\" = \"$$\" ] && echo leader; \
                     env | sort";

const ALLOWED: [&str; 9] = [
    "PATH", "HOME", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "USER", "SHELL", "TMPDIR",
];

/// What `PROBE` prints for a shell started with `zero` as `$0` in `root`.
fn expected(zero: &str, root: &str) -> String {
    let mut env: BTreeSet<String> = (ALLOWED.iter())
        .filter_map(|key| Some(format!("{key}={}", std::env::var(key).ok()?)))
        .collect();
    env.insert(format!("PWD={root}"));
    let env: Vec<String> = env.into_iter().collect();
    format!(
        "0={zero} #=0\n{root}\nstdin-eof\nleader\n{}\n[exit code 0]",
        env.join("\n")
    )
}

#[tokio::test]
async fn a_local_foreground_command_runs_as_before() {
    let ws = Workspace::new();
    let tool = Bash::new(ws.root());
    let out = text(call(&tool, json!({"command": PROBE})).await);
    let root = ws.root().display().to_string();
    assert_eq!(out, expected("/bin/sh", &root));
}

#[tokio::test]
async fn a_local_background_command_runs_as_before() {
    let ws = Workspace::new();
    let processes = Arc::new(Processes::new());
    let tool = Bash::new(ws.root()).with_processes(Arc::clone(&processes));
    let started = tool
        .execute(
            common::input(json!({"command": PROBE, "run_in_background": true})),
            ctx("s1"),
        )
        .await
        .unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    let output = BashOutput::new(processes);
    let read = json!({"process_id": id, "wait_ms": 10_000});
    let out = text(output.execute(common::input(read), ctx("s1")).await);
    // The inner shell of the lifeline wrapper: `$0` is `/bin/sh` again.
    let root = ws.root().display().to_string();
    assert_eq!(out, expected("/bin/sh", &root));
    tokio::time::sleep(Duration::from_millis(10)).await;
}
