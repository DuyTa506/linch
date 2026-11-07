//! `Bash`: process groups, timeout, cancel, output caps, environment.
//!
//! Orphan checks start a background job that would write a marker file a
//! second later; the test then waits past that point and checks the marker
//! is absent. That holds however (or whether) the system reaps zombies.

mod common;

use std::path::Path;
use std::time::{Duration, Instant};

use common::{Workspace, call, ctx, input, text};
use linch_core::{Tool, ToolResult, ToolScope};
use linch_tools::Bash;
use serde_json::{Value, json};

/// A background job that leaves `marker` behind if it outlives the command.
const LEAK: &str = "(sleep 1; echo leaked > marker) &";

fn bash(ws: &Workspace) -> Bash {
    Bash::new(ws.root())
}

async fn run(tool: &Bash, command: &str, timeout_ms: u64) -> ToolResult {
    let value = json!({"command": command, "timeout_ms": timeout_ms});
    call(tool, value).await
}

/// Wait past the leak's deadline and check nothing it would write exists.
async fn assert_no_leak(root: &Path) {
    tokio::time::sleep(Duration::from_millis(1_500)).await;
    assert!(
        !root.join("marker").exists(),
        "a background process survived"
    );
}

#[test]
fn bash_is_exec_scoped_and_clamps_its_timeout() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    assert_eq!(tool.scope(), ToolScope::Exec);
    let checked = tool
        .validate(input(json!({"command": "true", "timeout_ms": 99_999_999})))
        .unwrap();
    assert_eq!(checked["timeout_ms"], json!(1_800_000));
    assert_eq!(
        tool.summarize(&input(json!({"command": "ls -la"}))),
        "Bash: ls -la"
    );
}

#[tokio::test]
async fn bash_reports_stdout_stderr_and_exit_status() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let out = text(run(&tool, "echo out; echo err >&2", 5_000).await);
    assert_eq!(out, "out\n[stderr]\nerr\n[exit code 0]");
    let failed = run(&tool, "echo partial; exit 3", 5_000).await.unwrap_err();
    assert_eq!(failed.code.as_deref(), Some("exit_status"));
    assert_eq!(failed.message, "partial\n[exit code 3]");
    assert_eq!(failed.details, Some(json!({"exit_code": 3})));
    let killed = run(&tool, "kill -9 $$", 5_000).await.unwrap_err();
    assert_eq!(killed.message, "[killed by signal 9]");
    assert_eq!(killed.details, Some(json!({"signal": 9})));
}

#[tokio::test]
async fn bash_runs_in_the_root_with_a_closed_stdin() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let pwd = text(run(&tool, "pwd", 5_000).await);
    assert_eq!(pwd, format!("{}\n[exit code 0]", ws.root().display()));
    let stdin = text(run(&tool, "cat; echo eof", 5_000).await);
    assert_eq!(stdin, "eof\n[exit code 0]");
}

#[tokio::test]
async fn bash_passes_only_allowlisted_and_configured_environment() {
    let ws = Workspace::new();
    let tool = bash(&ws).with_env([("LINCH_TEST_VAR".to_owned(), "yes".to_owned())]);
    let out = text(run(&tool, "env | cut -d= -f1 | sort", 5_000).await);
    let allowed = [
        "PATH",
        "HOME",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "SHELL",
        "TMPDIR",
        "LINCH_TEST_VAR",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
    ];
    let names: Vec<&str> = out.lines().filter(|line| !line.starts_with('[')).collect();
    assert!(names.contains(&"LINCH_TEST_VAR"), "{names:?}");
    for name in names {
        assert!(allowed.contains(&name), "{name} leaked into Bash");
    }
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn a_configured_secret_reaches_the_environment_but_not_the_argv() {
    let ws = Workspace::new();
    let tool = bash(&ws).with_env([("LINCH_SECRET".to_owned(), "s3cr3t-value".to_owned())]);
    let command = r#"printf '%s' "$LINCH_SECRET" | wc -c; tr '\0' ' ' < /proc/$$/cmdline"#;
    let out = text(run(&tool, command, 5_000).await);
    assert!(out.starts_with("12"), "the child sees the value: {out}");
    assert!(!out.contains("s3cr3t-value"), "the value is in argv: {out}");
}

#[tokio::test]
async fn timeout_kills_the_whole_group_and_keeps_partial_output() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let started = Instant::now();
    let command = format!("{LEAK} echo before; sleep 30");
    let error = run(&tool, &command, 300).await.unwrap_err();
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(error.code.as_deref(), Some("timeout"));
    assert_eq!(
        error.message,
        "before\n[timed out after 300 ms; process group killed]"
    );
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn a_call_deadline_cuts_the_timeout_and_keeps_partial_output() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let deadline = tokio::time::Instant::now() + Duration::from_millis(600);
    let call_ctx = ctx("s1").with_deadline(Some(deadline));
    let command = json!({"command": format!("{LEAK} echo before; sleep 30")});
    // The scheduler drops the call at its deadline: Bash must answer first.
    let call = tool.execute(input(command), call_ctx);
    let result = tokio::time::timeout_at(deadline, call).await;
    let error = result.expect("Bash ends before the deadline").unwrap_err();
    assert_eq!(error.code.as_deref(), Some("timeout"));
    let message = error.message;
    assert!(
        message.starts_with("before\n[timed out after "),
        "{message}"
    );
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn cancel_kills_the_whole_group() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let call_ctx = ctx("s1");
    let cancel = call_ctx.cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        cancel.cancel();
    });
    let command = json!({"command": format!("{LEAK} sleep 30")});
    let started = Instant::now();
    let error = tool.execute(input(command), call_ctx).await.unwrap_err();
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(error.code.as_deref(), Some("aborted"));
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn a_cancelled_call_starts_nothing() {
    let ws = Workspace::new();
    let call_ctx = ctx("s1");
    call_ctx.cancel.cancel();
    let command = json!({"command": "echo started > marker"});
    let error = bash(&ws)
        .execute(input(command), call_ctx)
        .await
        .unwrap_err();
    assert_eq!(error.code.as_deref(), Some("aborted"));
    assert!(!ws.root().join("marker").exists());
}

#[tokio::test]
async fn dropping_the_call_kills_the_whole_group() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    // The scheduler's call timeout drops the future the same way.
    let command = json!({"command": format!("{LEAK} sleep 30")});
    let call = tool.execute(input(command), ctx("s1"));
    assert!(
        tokio::time::timeout(Duration::from_millis(300), call)
            .await
            .is_err()
    );
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn background_jobs_die_when_the_command_exits() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    let started = Instant::now();
    let out = text(run(&tool, &format!("{LEAK} echo done"), 5_000).await);
    assert_eq!(out, "done\n[exit code 0]");
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_no_leak(&ws.root()).await;
}

#[tokio::test]
async fn each_stream_keeps_its_first_4000_and_last_26000_bytes() {
    let ws = Workspace::new();
    let tool = bash(&ws);
    // `seq 1 30000` writes 168894 bytes.
    let out = text(run(&tool, "seq 1 30000; seq 1 30000 >&2", 10_000).await);
    let (stdout, stderr) = out.split_once("\n[stderr]\n").unwrap();
    for stream in [stdout, stderr.strip_suffix("\n[exit code 0]").unwrap()] {
        let (head, rest) = stream.split_once("\n… (138894 bytes omitted)\n").unwrap();
        assert_eq!(head.len(), 4_000);
        assert!(head.starts_with("1\n2\n3\n"));
        assert_eq!(
            rest.len() + 1,
            26_000,
            "the tail ends with the dropped newline"
        );
        assert!(rest.ends_with("29999\n30000"));
    }
}

#[tokio::test]
async fn small_output_is_kept_whole() {
    let ws = Workspace::new();
    let out = call(&bash(&ws), json!({"command": "seq 1 3"}))
        .await
        .unwrap();
    assert_eq!(out.value, Value::from("1\n2\n3\n[exit code 0]"));
    assert_eq!(out.metadata["exit_code"], json!(0));
    assert_eq!(out.metadata["truncated"], json!(false));
}
