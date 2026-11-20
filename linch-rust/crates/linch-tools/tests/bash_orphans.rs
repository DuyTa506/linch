//! What a command starts must end with the command: when it exits, times
//! out, is killed, its session closes, its registry drops, or the server
//! dies, nothing it started is left running, including a process that
//! left the command's process group (`setsid`, `setpgid`) or was handed to
//! `init` (a double fork). Tested on `local` and `sandbox` (skipped, with
//! a note, where the kernel cannot enforce the sandbox).
//!
//! The helper (`linch-reaper`) is what holds a command's descendants, and
//! it is off unless the host turns it on, so every test turns it on first.
//!
//! Every test records the pids it expects to end and kills them by pid
//! when it finishes, even by a panic, so a failure leaves nothing behind.

#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{Workspace, call, code, ctx, input};
use linch_core::{SessionId, Tool};
use linch_tools::reaper::{ReaperCommand, register};
use linch_tools::{Bash, BashKill, ExecutionBackend, ExecutionConfig, Processes, SandboxPolicy};
use serde_json::json;

/// Turn the helper on for this test process, as a host that ships it would:
/// commands run without it unless something does.
fn use_the_helper() {
    let _ = register(ReaperCommand {
        program: env!("CARGO_BIN_EXE_linch-reaper").into(),
        args: Vec::new(),
    });
}

/// A temp workspace, with the helper on.
fn workspace() -> Workspace {
    use_the_helper();
    Workspace::new()
}

/// Kills the pids it holds when dropped.
#[derive(Default)]
struct Pids(Vec<u32>);

impl Drop for Pids {
    fn drop(&mut self) {
        for pid in &self.0 {
            let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
        }
    }
}

/// Whether process `pid` runs (a zombie does not count).
fn alive(pid: u32) -> bool {
    match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => !stat
            .rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z')),
        Err(_) => false,
    }
}

/// The pid a command wrote to `root/name`, waited for up to 10 s.
fn written(root: &Path, name: &str) -> u32 {
    for _ in 0..500 {
        if let Ok(text) = fs::read_to_string(root.join(name))
            && let Ok(pid) = text.trim().parse()
        {
            return pid;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("the command never wrote {name}");
}

/// [`written`] for a test that must let its other tasks run meanwhile.
async fn written_async(root: &Path, name: &str) -> u32 {
    for _ in 0..500 {
        if let Ok(text) = fs::read_to_string(root.join(name))
            && let Ok(pid) = text.trim().parse()
        {
            return pid;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("the command never wrote {name}");
}

/// Wait up to 3 s for `pid` to end.
async fn ends(pid: u32) -> bool {
    let until = Instant::now() + Duration::from_secs(3);
    while Instant::now() < until {
        if !alive(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

fn sandbox(ws: &Workspace) -> Option<ExecutionBackend> {
    let config = ExecutionConfig::Sandbox(SandboxPolicy::default());
    match ExecutionBackend::open(ws.fs.clone(), &config) {
        Ok(backend) => Some(backend),
        Err(error) => {
            eprintln!("skipped: {error}");
            None
        }
    }
}

/// A command that starts in a new session what must end with it.
const DETACH: &str = "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1";

/// A process that leaves the group with `setpgid`, where python is there.
const REGROUP: &str = "python3 -c \"import os,time; os.setpgid(0,0); open('pid','w').write(str(os.getpid())); time.sleep(3117)\"";

/// A daemon: forks, starts a new session, forks again, the parents exit.
const DAEMON: &str =
    "(setsid sh -c 'sh -c \"echo \\$\\$ > pid; exec sleep 3117\" &') >/dev/null 2>&1";

/// A process that ignores everything but `SIGKILL`.
const STUBBORN: &str = "setsid sh -c \"trap '' TERM HUP INT; echo \\$\\$ > pid; while :; do sleep 1; done\" >/dev/null 2>&1";

fn have_python() -> bool {
    Path::new("/usr/bin/python3").exists()
}

// ---------------------------------------------------------------------
// local, foreground
// ---------------------------------------------------------------------

/// The command ends on its own and leaves a detached process behind.
#[tokio::test]
async fn nothing_a_local_command_started_outlives_the_call() {
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    let command = format!("{DETACH} & until [ -s pid ]; do sleep 0.02; done; echo done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "process {pid} (setsid) ran on after the call"
    );
}

#[tokio::test]
async fn nothing_a_local_command_started_outlives_its_timeout() {
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    // The detached process is the shell's child, which waits for it.
    let result = call(&tool, json!({"command": DETACH, "timeout_ms": 1_000})).await;
    assert_eq!(code(result), "timeout");
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "process {pid} (setsid) ran on after the timeout"
    );
}

#[tokio::test]
async fn a_local_process_that_changes_its_group_ends_with_the_call() {
    if !have_python() {
        return eprintln!("skipped: no python3");
    }
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    let command = format!("{REGROUP} & until [ -s pid ]; do sleep 0.02; done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "process {pid} (setpgid) ran on after the call"
    );
}

#[tokio::test]
async fn a_local_double_forked_daemon_ends_with_the_call() {
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    let command = format!("{DAEMON}; until [ -s pid ]; do sleep 0.02; done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "daemon {pid} ran on after the call");
}

#[tokio::test]
async fn a_local_process_that_ignores_sigterm_ends_with_the_call() {
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    let command = format!("{STUBBORN} & until [ -s pid ]; do sleep 0.02; done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ignored the kill");
}

/// A call that is cancelled mid-run ends what it detached too.
#[tokio::test]
async fn a_cancelled_local_call_ends_what_it_detached() {
    let ws = workspace();
    let tool = Bash::new(ws.root());
    let mut pids = Pids::default();
    let context = ctx("s1");
    let cancel = context.cancel.clone();
    let value = json!({"command": DETACH, "timeout_ms": 30_000});
    let running = tokio::spawn(async move { tool.execute(input(value), context).await });
    let pid = written_async(&ws.root(), "pid").await;
    pids.0.push(pid);
    cancel.cancel();
    let _ = running.await;
    assert!(ends(pid).await, "process {pid} ran on after the cancel");
}

// ---------------------------------------------------------------------
// local, background
// ---------------------------------------------------------------------

struct Background {
    processes: Arc<Processes>,
    bash: Bash,
    kill: BashKill,
}

fn background(ws: &Workspace) -> Background {
    let processes = Arc::new(Processes::new());
    Background {
        bash: Bash::new(ws.root()).with_processes(Arc::clone(&processes)),
        kill: BashKill::new(Arc::clone(&processes)),
        processes,
    }
}

/// Start `command` in the background in session `s1`; the process id.
async fn start(bash: &Bash, command: &str) -> String {
    let value = json!({"command": command, "run_in_background": true});
    let started = call(bash, value).await.expect("started");
    started.metadata["process_id"].as_str().unwrap().to_owned()
}

const BACKGROUND: &str =
    "setsid sh -c 'echo $$ > pid; exec sleep 3117' >/dev/null 2>&1 & sleep 3117";

#[tokio::test]
async fn bash_kill_ends_what_a_background_command_detached() {
    let ws = workspace();
    let tools = background(&ws);
    let mut pids = Pids::default();
    let id = start(&tools.bash, BACKGROUND).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    let _ = call(&tools.kill, json!({"process_id": id})).await;
    assert!(ends(pid).await, "process {pid} ran on after BashKill");
}

#[tokio::test]
async fn closing_the_session_ends_what_a_background_command_detached() {
    let ws = workspace();
    let tools = background(&ws);
    let mut pids = Pids::default();
    let _ = start(&tools.bash, BACKGROUND).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    tools.processes.close_session(&SessionId::from("s1"));
    assert!(ends(pid).await, "process {pid} ran on after close_session");
}

#[tokio::test]
async fn dropping_the_registry_ends_what_a_background_command_detached() {
    let ws = workspace();
    let tools = background(&ws);
    let mut pids = Pids::default();
    let _ = start(&tools.bash, BACKGROUND).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    let Background {
        processes,
        bash,
        kill,
    } = tools;
    drop((bash, kill, processes));
    assert!(
        ends(pid).await,
        "process {pid} ran on after the registry dropped"
    );
}

#[tokio::test]
async fn a_background_timeout_ends_what_the_command_detached() {
    let ws = workspace();
    let tools = background(&ws);
    let mut pids = Pids::default();
    let value = json!({"command": BACKGROUND, "run_in_background": true, "timeout_ms": 800});
    let _ = call(&tools.bash, value).await.expect("started");
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(ends(pid).await, "process {pid} ran on after the timeout");
}

#[tokio::test]
async fn a_background_command_that_exits_ends_what_it_detached() {
    let ws = workspace();
    let tools = background(&ws);
    let mut pids = Pids::default();
    let command = format!("{DETACH} & until [ -s pid ]; do sleep 0.02; done");
    let _ = start(&tools.bash, &command).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "process {pid} ran on after the command exited"
    );
}

/// The server killed by `SIGKILL`: a helper process (this test binary, run
/// again) hosts a background command that detaches a process; killing the
/// helper must end both. The helper does nothing unless it is asked to.
#[tokio::test]
#[ignore = "run by the_server_dying_ends_what_its_background_commands_detached"]
async fn server_for_the_dying_test() {
    let Ok(root) = std::env::var("LINCH_ORPHAN_SERVER_ROOT") else {
        return;
    };
    use_the_helper();
    let tools = Arc::new(Processes::new());
    let bash = Bash::new(&root).with_processes(Arc::clone(&tools));
    let _ = start(&bash, BACKGROUND).await;
    // Wait to be killed; stop by itself if nobody comes.
    tokio::time::sleep(Duration::from_secs(60)).await;
}

#[tokio::test]
async fn the_server_dying_ends_what_its_background_commands_detached() {
    let ws = workspace();
    let mut pids = Pids::default();
    let mut server = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "server_for_the_dying_test",
            "--ignored",
            "--nocapture",
        ])
        .env("LINCH_ORPHAN_SERVER_ROOT", ws.root())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    pids.0.push(server.id());
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(alive(pid));
    server.kill().unwrap();
    server.wait().unwrap();
    assert!(
        ends(pid).await,
        "process {pid} ran on after the server died"
    );
}

// ---------------------------------------------------------------------
// sandbox
// ---------------------------------------------------------------------

/// "A timeout kills the whole process tree, no orphan", and a session's
/// close ends its commands.
#[tokio::test]
async fn nothing_a_sandboxed_command_started_outlives_its_timeout_and_session() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    let mut pids = Pids::default();
    let tool = Bash::in_backend(&backend);
    let result = call(&tool, json!({"command": DETACH, "timeout_ms": 1_000})).await;
    assert_eq!(code(result), "timeout");
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    backend.close_session(&SessionId::from("s1"));
    assert!(
        ends(pid).await,
        "sandboxed process {pid} (setsid) ran on after its timeout and its session's close"
    );
}

#[tokio::test]
async fn nothing_a_sandboxed_command_started_outlives_the_call() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    let mut pids = Pids::default();
    let tool = Bash::in_backend(&backend);
    let command = format!("{DETACH} & until [ -s pid ]; do sleep 0.02; done; echo done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "sandboxed process {pid} ran on after the call"
    );
}

#[tokio::test]
async fn a_sandboxed_daemon_and_a_stubborn_process_end_with_the_call() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    let mut pids = Pids::default();
    let tool = Bash::in_backend(&backend);
    let command = format!("{DAEMON}; {STUBBORN} & until [ -s pid ]; do sleep 0.02; done");
    let _ = call(&tool, json!({"command": command, "timeout_ms": 10_000})).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    assert!(
        ends(pid).await,
        "sandboxed process {pid} ran on after the call"
    );
}

#[tokio::test]
async fn closing_the_session_ends_what_a_sandboxed_background_command_detached() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    let mut pids = Pids::default();
    let processes = Arc::new(Processes::new());
    let tool = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    let _ = start(&tool, BACKGROUND).await;
    let pid = written(&ws.root(), "pid");
    pids.0.push(pid);
    processes.close_session(&SessionId::from("s1"));
    assert!(
        ends(pid).await,
        "sandboxed process {pid} ran on after close_session"
    );
}

// ---------------------------------------------------------------------
// the helper is stopped, by the command or from outside
// ---------------------------------------------------------------------

/// `rest`, after refusing to run unless this shell's parent (`$PPID`) is
/// the helper and not this test process, which `kill -9 $PPID` would take
/// down; then it records the shell's pid (`shell`), its parent's
/// (`helper`) and a child's (`pid`).
fn under_a_helper(rest: &str) -> String {
    format!(
        "[ \"$PPID\" != {} ] || exit 9; echo $$ > shell; echo $PPID > helper; \
         sleep 3117 & echo $! > pid; {rest}",
        std::process::id()
    )
}

/// The shell and its child the command wrote must end.
async fn the_command_ends(root: &Path, pids: &mut Pids, when: &str) {
    for name in ["shell", "pid"] {
        let pid = written_async(root, name).await;
        pids.0.push(pid);
        assert!(ends(pid).await, "process {pid} ({name}) ran on {when}");
    }
}

/// How long a call whose helper was killed may take to return.
const PROMPT: Duration = Duration::from_secs(6);

/// The command kills its own helper and goes on waiting.
async fn a_command_kills_its_helper(tool: Bash, root: &Path) {
    let mut pids = Pids::default();
    let command = under_a_helper("kill -9 $PPID; echo $? > signalled; sleep 3117");
    let started = Instant::now();
    let _ = call(&tool, json!({"command": command, "timeout_ms": 1_000})).await;
    assert!(
        started.elapsed() < PROMPT,
        "the call did not return in time"
    );
    the_command_ends(root, &mut pids, "after the command killed its helper").await;
}

/// A background job of the command kills the helper while the foreground
/// one waits.
async fn a_background_job_kills_the_helper(tool: Bash, root: &Path) {
    let mut pids = Pids::default();
    let command = under_a_helper("PP=$PPID; (sleep 0.3; kill -9 $PP) & sleep 3117");
    let started = Instant::now();
    let _ = call(&tool, json!({"command": command, "timeout_ms": 20_000})).await;
    assert!(
        started.elapsed() < PROMPT,
        "the call did not return in time"
    );
    the_command_ends(
        root,
        &mut pids,
        "after a job of the command killed its helper",
    )
    .await;
}

/// The helper is killed from outside while the call waits.
async fn the_helper_is_killed_from_outside(tool: Bash, root: &Path) {
    let mut pids = Pids::default();
    let command = under_a_helper("wait");
    let running = tokio::spawn(async move {
        let value = json!({"command": command, "timeout_ms": 20_000});
        tool.execute(input(value), ctx("s1")).await
    });
    let helper = written_async(root, "helper").await;
    assert_ne!(helper, std::process::id(), "no helper under the command");
    pids.0.push(helper);
    let _ = written_async(root, "pid").await;
    let _ = Command::new("kill")
        .args(["-9", &helper.to_string()])
        .status();
    let _ = tokio::time::timeout(PROMPT, running)
        .await
        .expect("the call did not return after its helper was killed");
    the_command_ends(root, &mut pids, "after the helper was killed").await;
}

#[tokio::test]
async fn a_local_command_that_kills_its_helper_still_ends_with_the_call() {
    let ws = workspace();
    a_command_kills_its_helper(Bash::new(ws.root()), &ws.root()).await;
}

#[tokio::test]
async fn a_local_job_that_kills_the_helper_still_ends_with_the_call() {
    let ws = workspace();
    a_background_job_kills_the_helper(Bash::new(ws.root()), &ws.root()).await;
}

#[tokio::test]
async fn a_local_helper_killed_from_outside_still_ends_the_command() {
    let ws = workspace();
    the_helper_is_killed_from_outside(Bash::new(ws.root()), &ws.root()).await;
}

#[tokio::test]
async fn a_sandboxed_command_that_kills_its_helper_still_ends_with_the_call() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    a_command_kills_its_helper(Bash::in_backend(&backend), &ws.root()).await;
    let signalled = fs::read_to_string(ws.root().join("signalled")).unwrap_or_default();
    eprintln!(
        "a sandboxed `kill -9 $PPID` exited with {:?}",
        signalled.trim()
    );
}

#[tokio::test]
async fn a_sandboxed_job_that_kills_the_helper_still_ends_with_the_call() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    a_background_job_kills_the_helper(Bash::in_backend(&backend), &ws.root()).await;
}

#[tokio::test]
async fn a_sandboxed_helper_killed_from_outside_still_ends_the_command() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    the_helper_is_killed_from_outside(Bash::in_backend(&backend), &ws.root()).await;
}

// ---------------------------------------------------------------------
// the pid a background command reports
// ---------------------------------------------------------------------

/// The pid is the shell's: signalling it stops the command, as it does
/// without a helper.
async fn signalling_the_reported_pid_stops_the_command(tool: Bash, root: &Path) {
    let mut pids = Pids::default();
    let tool = tool.with_processes(Arc::new(Processes::new()));
    let value = json!({"command": under_a_helper("wait"), "run_in_background": true});
    let started = call(&tool, value).await.expect("started");
    let reported = started.metadata["pid"].as_u64().expect("a pid");
    let shell = written_async(root, "shell").await;
    let child = written_async(root, "pid").await;
    pids.0.extend([shell, child]);
    assert_eq!(
        reported,
        u64::from(shell),
        "the reported pid is not the shell's"
    );
    let _ = Command::new("kill")
        .args(["-TERM", &reported.to_string()])
        .status();
    assert!(
        ends(shell).await,
        "the shell ran on after its pid was signalled"
    );
    assert!(
        ends(child).await,
        "process {child} ran on after its shell was signalled"
    );
}

#[tokio::test]
async fn signalling_the_pid_of_a_local_background_command_stops_it() {
    let ws = workspace();
    signalling_the_reported_pid_stops_the_command(Bash::new(ws.root()), &ws.root()).await;
}

#[tokio::test]
async fn signalling_the_pid_of_a_sandboxed_background_command_stops_it() {
    let ws = workspace();
    let Some(backend) = sandbox(&ws) else {
        return;
    };
    signalling_the_reported_pid_stops_the_command(Bash::in_backend(&backend), &ws.root()).await;
}
