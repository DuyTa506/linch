//! Background `Bash`: `run_in_background`, `BashOutput` and `BashKill`
//! over one per-agent `Processes` registry, keyed by session.
//!
//! Reaping is checked on `/proc/<pid>` of the shell: a zombie keeps its
//! entry there, so a gone entry means the supervisor waited for it. Leak
//! checks use a job that would write a marker file a second later.

mod common;

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{Workspace, code, ctx, input, text};
use linch_core::{JsonObject, Tool, ToolOutput, ToolScope};
use linch_tools::{Bash, BashKill, BashOutput, MAX_RUNNING, NO_SUCH_PROCESS_CODE, Processes};
use serde_json::{Value, json};

/// A background job that leaves `marker` behind if it outlives its group.
const LEAK: &str = "(sleep 1; echo leaked > marker) &";

struct Tools {
    processes: Arc<Processes>,
    bash: Bash,
    output: BashOutput,
    kill: BashKill,
}

fn tools(ws: &Workspace) -> Tools {
    let processes = Arc::new(Processes::new());
    Tools {
        bash: Bash::new(ws.root()).with_processes(Arc::clone(&processes)),
        output: BashOutput::new(Arc::clone(&processes)),
        kill: BashKill::new(Arc::clone(&processes)),
        processes,
    }
}

async fn call_in(tool: &dyn Tool, session: &str, value: Value) -> linch_core::ToolResult {
    tool.execute(input(value), ctx(session)).await
}

/// Start `command` in the background in session `s1`; its id and pid.
async fn start(tools: &Tools, command: &str) -> (String, u32) {
    let value = json!({"command": command, "run_in_background": true});
    let started = call_in(&tools.bash, "s1", value).await.expect("started");
    ids(&started)
}

fn ids(output: &ToolOutput) -> (String, u32) {
    let meta: &JsonObject = &output.metadata;
    let id = meta["process_id"].as_str().unwrap().to_owned();
    let pid = meta["pid"].as_u64().unwrap() as u32;
    (id, pid)
}

async fn read(tools: &Tools, id: &str, wait_ms: u64) -> linch_core::ToolResult {
    let value = json!({"process_id": id, "wait_ms": wait_ms});
    call_in(&tools.output, "s1", value).await
}

/// Whether the shell is gone from the process table: exited and reaped.
fn reaped(pid: u32) -> bool {
    // Without `/proc` a missing entry would read as reaped for any pid, so
    // other systems ask `ps`, which lists a zombie too.
    #[cfg(target_os = "linux")]
    {
        !Path::new(&format!("/proc/{pid}")).exists()
    }
    #[cfg(not(target_os = "linux"))]
    {
        let ps = std::process::Command::new("ps")
            .args(["-o", "stat=", "-p", &pid.to_string()])
            .output()
            .expect("run ps");
        String::from_utf8_lossy(&ps.stdout).trim().is_empty()
    }
}

async fn eventually_reaped(pid: u32) {
    let until = Instant::now() + Duration::from_secs(5);
    while !reaped(pid) {
        assert!(Instant::now() < until, "process {pid} was not reaped");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

async fn assert_no_leak(root: &Path) {
    tokio::time::sleep(Duration::from_millis(1_500)).await;
    assert!(!root.join("marker").exists(), "a background job survived");
}

#[test]
fn reading_is_read_scope_and_killing_is_exec_scope() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    assert_eq!(tools.output.scope(), ToolScope::Read);
    assert!(tools.output.parallel(&JsonObject::new()));
    assert_eq!(tools.kill.scope(), ToolScope::Exec);
    let schema = tools.bash.input_schema();
    assert!(schema["properties"]["run_in_background"].is_object());
}

#[tokio::test]
async fn a_background_command_returns_at_once_and_its_output_is_read_in_parts() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let begun = Instant::now();
    let (id, pid) = start(&tools, "echo first; sleep 0.3; echo second >&2; exit 3").await;
    assert!(begun.elapsed() < Duration::from_millis(250));
    assert!(id.starts_with("bash_"), "{id}");

    tokio::time::sleep(Duration::from_millis(100)).await;
    let running = read(&tools, &id, 0).await.unwrap();
    assert_eq!(running.metadata["status"], "running");
    let first = running.value.as_str().unwrap();
    assert!(first.starts_with("first\n["), "{first}");

    let ended = read(&tools, &id, 5_000).await.unwrap_err();
    assert_eq!(ended.code.as_deref(), Some("exit_status"));
    assert_eq!(ended.message, "[stderr]\nsecond\n[exit code 3]");
    assert!(reaped(pid));
    // Read again: nothing new, the same end.
    let again = read(&tools, &id, 0).await.unwrap_err();
    assert_eq!(again.message, "[exit code 3]");
}

#[tokio::test]
async fn a_clean_exit_is_a_success() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, _) = start(&tools, "echo done").await;
    let out = read(&tools, &id, 5_000).await.unwrap();
    assert_eq!(out.value, "done\n[exit code 0]");
    assert_eq!(out.metadata["status"], "exited");
    assert_eq!(out.metadata["exit_code"], 0);
}

#[tokio::test]
async fn kill_stops_the_whole_group_and_reaps_the_shell() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, pid) = start(&tools, &format!("{LEAK} echo up; sleep 30")).await;
    assert!(!reaped(pid));
    let killed = call_in(&tools.kill, "s1", json!({"process_id": id})).await;
    let killed = killed.unwrap();
    assert!(reaped(pid), "BashKill returns once the shell is reaped");
    assert_eq!(killed.metadata["status"], "killed");
    let text = killed.value.as_str().unwrap();
    assert!(text.ends_with("[killed; process group killed]"), "{text}");
    assert_eq!(tools.processes.running(&"s1".into()), 0);
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn a_background_timeout_kills_the_group() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let value = json!({"command": format!("{LEAK} sleep 30"), "run_in_background": true,
                       "timeout_ms": 200});
    let (id, pid) = ids(&call_in(&tools.bash, "s1", value).await.unwrap());
    let ended = read(&tools, &id, 5_000).await.unwrap_err();
    assert_eq!(ended.code.as_deref(), Some("timeout"));
    assert!(
        ended.message.contains("timed out after 200 ms"),
        "{}",
        ended.message
    );
    assert!(reaped(pid));
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn a_process_id_is_known_only_to_its_session_and_registry() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, _) = start(&tools, "sleep 30").await;
    let other = call_in(&tools.output, "s2", json!({"process_id": id})).await;
    assert_eq!(code(other), NO_SUCH_PROCESS_CODE);
    let kill = call_in(&tools.kill, "s2", json!({"process_id": id})).await;
    assert_eq!(code(kill), NO_SUCH_PROCESS_CODE);
    // A registry after a restart has never heard of it.
    let restarted = tools_after_restart(&ws);
    let lost = call_in(&restarted.output, "s1", json!({"process_id": id})).await;
    let error = lost.unwrap_err();
    assert_eq!(error.code.as_deref(), Some(NO_SUCH_PROCESS_CODE));
    assert!(
        error.message.contains("no longer exists"),
        "{}",
        error.message
    );
    assert!(error.message.contains("restart"), "{}", error.message);
}

/// Session B names session A's process to both tools: each is
/// `no_such_process`, and A's process runs on, its output unread.
#[tokio::test]
async fn another_session_can_neither_read_nor_kill_a_process() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, pid) = start(&tools, "echo from-a; sleep 30").await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    for tool in [&tools.output as &dyn Tool, &tools.kill] {
        let error = call_in(tool, "s2", json!({"process_id": id}))
            .await
            .unwrap_err();
        assert_eq!(
            error.code.as_deref(),
            Some(NO_SUCH_PROCESS_CODE),
            "{}",
            tool.name()
        );
        assert!(!error.message.contains("from-a"), "{}", error.message);
    }
    assert!(!reaped(pid), "A's process runs on");
    assert_eq!(tools.processes.running(&"s1".into()), 1);
    let mine = read(&tools, &id, 0).await.unwrap();
    assert_eq!(mine.metadata["status"], "running");
    assert!(
        mine.value.as_str().unwrap().starts_with("from-a\n"),
        "{:?}",
        mine.value
    );
    tools.processes.close();
    eventually_reaped(pid).await;
}

fn tools_after_restart(ws: &Workspace) -> Tools {
    tools(ws)
}

#[tokio::test]
async fn closing_the_session_kills_and_reaps_its_processes_only() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (_, mine) = start(&tools, &format!("{LEAK} sleep 30")).await;
    let value = json!({"command": "sleep 30", "run_in_background": true});
    let (theirs_id, theirs) = ids(&call_in(&tools.bash, "s2", value).await.unwrap());
    tools.processes.close_session(&"s1".into());
    eventually_reaped(mine).await;
    assert!(!reaped(theirs), "another session's process runs on");
    let still = call_in(&tools.output, "s2", json!({"process_id": theirs_id})).await;
    assert_eq!(still.unwrap().metadata["status"], "running");
    assert_no_leak(&ws.root()).await;
    tools.processes.close();
    eventually_reaped(theirs).await;
}

#[tokio::test]
async fn dropping_the_registry_kills_and_reaps_every_process() {
    let ws = Workspace::new();
    let Tools {
        processes,
        bash,
        output,
        kill,
    } = tools(&ws);
    let value = json!({"command": format!("{LEAK} sleep 30"), "run_in_background": true});
    let (_, pid) = ids(&call_in(&bash, "s1", value).await.unwrap());
    drop((processes, bash, output, kill));
    eventually_reaped(pid).await;
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn a_session_runs_at_most_four_at_once() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let mut ids = Vec::new();
    for _ in 0..MAX_RUNNING {
        ids.push(start(&tools, "sleep 30").await.0);
    }
    let value = json!({"command": "sleep 30", "run_in_background": true});
    let full = call_in(&tools.bash, "s1", value.clone()).await;
    assert_eq!(code(full), "too_many_processes");
    // Another session has its own four.
    assert!(call_in(&tools.bash, "s2", value.clone()).await.is_ok());
    call_in(&tools.kill, "s1", json!({"process_id": ids[0]}))
        .await
        .unwrap();
    assert!(call_in(&tools.bash, "s1", value).await.is_ok());
    tools.processes.close();
}

#[tokio::test]
async fn unread_output_is_bounded_per_read() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, _) = start(&tools, "head -c 200000 /dev/zero | tr '\\0' a").await;
    let out = read(&tools, &id, 5_000).await.unwrap();
    assert_eq!(out.metadata["truncated"], true);
    let text = out.value.as_str().unwrap();
    assert!(text.len() < 31_000, "{}", text.len());
    assert!(text.contains("bytes omitted"));
}

#[tokio::test]
async fn without_a_registry_run_in_background_is_refused() {
    let ws = Workspace::new();
    let bash = Bash::new(ws.root());
    let value = json!({"command": "sleep 30", "run_in_background": true});
    let refused = call_in(&bash, "s1", value).await;
    assert_eq!(code(refused), "no_background");
}

#[tokio::test]
async fn the_foreground_is_unchanged() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let value = json!({"command": "echo hi", "run_in_background": false});
    let out = text(call_in(&tools.bash, "s1", value).await);
    assert_eq!(out, "hi\n[exit code 0]");
}

/// A failed command's status is in the error's details, where the call's
/// end event reads it (a `BashOutput` that read a failure has no metadata).
#[tokio::test]
async fn a_failed_background_command_reports_its_status_in_the_error_details() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let (id, _) = start(&tools, "echo out; exit 3").await;
    let failed = read(&tools, &id, 5_000).await.unwrap_err();
    assert_eq!(
        failed.details,
        Some(json!({"status": "exited", "exit_code": 3, "process_id": id, "truncated": false}))
    );

    let value = json!({"command": "sleep 30", "run_in_background": true, "timeout_ms": 200});
    let (id, _) = ids(&call_in(&tools.bash, "s1", value).await.unwrap());
    let timed_out = read(&tools, &id, 5_000).await.unwrap_err();
    assert_eq!(
        timed_out.details,
        Some(json!({"status": "timed_out", "process_id": id, "truncated": false}))
    );
}

/// The foreground names its status the same way: a success in the output's
/// metadata, a failure in the error's details.
#[tokio::test]
async fn a_foreground_command_reports_its_status() {
    let ws = Workspace::new();
    let tools = tools(&ws);
    let ok = call_in(&tools.bash, "s1", json!({"command": "echo hi"})).await;
    let ok = ok.unwrap();
    assert_eq!(ok.metadata["status"], "exited");
    assert_eq!(ok.metadata["exit_code"], 0);

    let failed = call_in(&tools.bash, "s1", json!({"command": "exit 3"})).await;
    assert_eq!(
        failed.unwrap_err().details,
        Some(json!({"status": "exited", "exit_code": 3, "truncated": false}))
    );
    let value = json!({"command": "sleep 30", "timeout_ms": 200});
    let timed_out = call_in(&tools.bash, "s1", value).await.unwrap_err();
    assert_eq!(
        timed_out.details,
        Some(json!({"status": "timed_out", "truncated": false}))
    );
}
