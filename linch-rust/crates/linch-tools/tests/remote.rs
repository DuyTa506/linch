//! The `remote` backend: `Bash` through a host that runs the commands.
//! `ShellHost` is a host in memory that runs real `/bin/sh` commands, so
//! these tests see real exits, output and kills.

mod common;

use std::collections::HashMap;
use std::future::pending;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use common::{Workspace, ctx, input, text};
use linch_core::{Tool, ToolResult};
use linch_tools::{
    Bash, BashKill, BashOutput, ExecutionBackend, ExecutionConfig, ExecutionError, Exit, Processes,
    RemoteChunk, RemoteCommand, RemoteError, RemoteExec, RemotePolicy, workspace_tools,
};
use rustix::process::{Pid, Signal, kill_process_group};
use serde_json::json;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Notify;

/// One command the host runs.
struct Running {
    pid: i32,
    out: Arc<Mutex<Vec<u8>>>,
    err: Arc<Mutex<Vec<u8>>>,
    exit: Arc<Mutex<Option<Exit>>>,
}

/// A host that runs commands as `/bin/sh -c` children of the test.
#[derive(Default)]
struct ShellHost {
    running: Mutex<HashMap<String, Running>>,
    started: Mutex<Vec<RemoteCommand>>,
    killed: Mutex<Vec<String>>,
    pids: Mutex<Vec<i32>>,
    changed: Arc<Notify>,
    /// Reads and kills fail with `Closed`, as a dropped connection's do.
    gone: AtomicBool,
}

impl std::fmt::Debug for ShellHost {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ShellHost")
    }
}

impl ShellHost {
    fn started(&self) -> Vec<RemoteCommand> {
        self.started.lock().unwrap().clone()
    }

    fn pid(&self, index: usize) -> i32 {
        self.pids.lock().unwrap()[index]
    }
}

fn alive(pid: i32) -> bool {
    // `kill -0` through the shell: no unsafe code here.
    std::process::Command::new("/bin/sh")
        .args(["-c", &format!("kill -0 {pid} 2>/dev/null")])
        .status()
        .unwrap()
        .success()
}

fn pump(
    mut pipe: impl tokio::io::AsyncRead + Unpin + Send + 'static,
    into: Arc<Mutex<Vec<u8>>>,
    changed: Arc<Notify>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut buffer = [0; 4096];
        while let Ok(read @ 1..) = pipe.read(&mut buffer).await {
            into.lock().unwrap().extend_from_slice(&buffer[..read]);
            changed.notify_waiters();
        }
    })
}

fn take(buffer: &Mutex<Vec<u8>>) -> String {
    String::from_utf8_lossy(&std::mem::take(&mut *buffer.lock().unwrap())).into_owned()
}

#[async_trait]
impl RemoteExec for ShellHost {
    async fn start(&self, command: RemoteCommand) -> Result<(), RemoteError> {
        self.started.lock().unwrap().push(command.clone());
        let mut shell = Command::new("/bin/sh");
        shell
            .args(["-c", &command.command])
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .envs(&command.env)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        if let Some(cwd) = &command.cwd {
            shell.current_dir(cwd);
        }
        let mut child = shell
            .spawn()
            .map_err(|error| RemoteError::Rejected(format!("spawn failed: {error}")))?;
        let pid = i32::try_from(child.id().unwrap()).unwrap();
        self.pids.lock().unwrap().push(pid);
        let running = Running {
            pid,
            out: Arc::default(),
            err: Arc::default(),
            exit: Arc::default(),
        };
        let out = pump(
            child.stdout.take().unwrap(),
            running.out.clone(),
            self.changed.clone(),
        );
        let err = pump(
            child.stderr.take().unwrap(),
            running.err.clone(),
            self.changed.clone(),
        );
        let (exit, changed) = (running.exit.clone(), self.changed.clone());
        tokio::spawn(async move {
            let status = child.wait().await.unwrap();
            // The exit is reported after the output it follows.
            let _ = tokio::join!(out, err);
            let end = match status.code() {
                Some(code) => Exit::Code(code),
                None => {
                    Exit::Signal(std::os::unix::process::ExitStatusExt::signal(&status).unwrap())
                }
            };
            *exit.lock().unwrap() = Some(end);
            changed.notify_waiters();
        });
        self.running.lock().unwrap().insert(command.id, running);
        Ok(())
    }

    async fn read(&self, id: &str, wait: Duration) -> Result<RemoteChunk, RemoteError> {
        let deadline = tokio::time::Instant::now() + wait;
        loop {
            if self.gone.load(Ordering::SeqCst) {
                return Err(RemoteError::Closed);
            }
            let notified = self.changed.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            {
                let mut running = self.running.lock().unwrap();
                let found = running
                    .get(id)
                    .ok_or_else(|| RemoteError::Rejected("no such command".into()))?;
                let exit = *found.exit.lock().unwrap();
                let pending_output =
                    !found.out.lock().unwrap().is_empty() || !found.err.lock().unwrap().is_empty();
                if pending_output || exit.is_some() {
                    let chunk = RemoteChunk {
                        stdout: take(&found.out),
                        stderr: take(&found.err),
                        exit,
                    };
                    if exit.is_some() {
                        running.remove(id);
                    }
                    return Ok(chunk);
                }
            }
            if tokio::time::timeout_at(deadline, notified).await.is_err() {
                return Ok(RemoteChunk::default());
            }
        }
    }

    async fn kill(&self, id: &str) -> Result<RemoteChunk, RemoteError> {
        self.killed.lock().unwrap().push(id.to_owned());
        if self.gone.load(Ordering::SeqCst) {
            return Err(RemoteError::Closed);
        }
        let found = self.running.lock().unwrap().remove(id);
        let Some(found) = found else {
            return Err(RemoteError::Rejected("no such command".into()));
        };
        kill_process_group(Pid::from_raw(found.pid).unwrap(), Signal::KILL).unwrap();
        for _ in 0..200 {
            if found.exit.lock().unwrap().is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let exit = *found.exit.lock().unwrap();
        Ok(RemoteChunk {
            stdout: take(&found.out),
            stderr: take(&found.err),
            exit,
        })
    }
}

fn backend(ws: &Workspace, policy: RemotePolicy, host: &Arc<ShellHost>) -> ExecutionBackend {
    let host: Arc<dyn RemoteExec> = host.clone();
    ExecutionBackend::open_with_host(ws.fs.clone(), &ExecutionConfig::Remote(policy), Some(host))
        .unwrap()
}

fn bash(ws: &Workspace) -> (Bash, Arc<ShellHost>, Arc<Processes>) {
    let host = Arc::new(ShellHost::default());
    let processes = Arc::new(Processes::new());
    let backend = backend(ws, RemotePolicy::default(), &host);
    let bash = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    (bash, host, processes)
}

async fn run(tool: &Bash, command: &str, timeout_ms: u64) -> ToolResult {
    let value = json!({"command": command, "timeout_ms": timeout_ms});
    tool.execute(input(value), ctx("s1")).await
}

fn message(result: ToolResult) -> String {
    result.expect_err("the call fails").message
}

#[tokio::test]
async fn a_remote_command_returns_its_output_and_exit_code() {
    let ws = Workspace::new();
    let (bash, host, _) = bash(&ws);
    let out = text(run(&bash, "echo out; echo err >&2", 10_000).await);
    assert_eq!(out, "out\n[stderr]\nerr\n[exit code 0]");
    let failed = run(&bash, "echo partial; exit 3", 10_000).await;
    assert_eq!(
        failed.as_ref().unwrap_err().code.as_deref(),
        Some("exit_status")
    );
    assert_eq!(message(failed), "partial\n[exit code 3]");
    let killed = run(&bash, "kill -9 $$", 10_000).await;
    assert_eq!(message(killed), "[killed by signal 9]");
    assert_eq!(host.started().len(), 3);
}

#[tokio::test]
async fn the_host_gets_no_server_identity_and_the_policys_cwd() {
    let ws = Workspace::new();
    let host = Arc::new(ShellHost::default());
    let policy = RemotePolicy {
        cwd: Some("/".into()),
        shared_workspace: false,
    };
    let bash = Bash::in_backend(&backend(&ws, policy, &host)).with_env([("K".into(), "v".into())]);
    let out = text(run(&bash, "pwd; echo $K", 10_000).await);
    assert_eq!(out, "/\nv\n[exit code 0]");
    let sent = &host.started()[0];
    assert_eq!(sent.cwd.as_deref(), Some("/"));
    assert!(!sent.background);
    let keys: Vec<&str> = sent.env.keys().map(String::as_str).collect();
    for server_only in ["PATH", "HOME", "USER", "SHELL", "TMPDIR"] {
        assert!(!keys.contains(&server_only), "{keys:?}");
    }
    assert_eq!(sent.env["K"], "v");
}

#[tokio::test]
async fn a_shared_workspace_starts_commands_in_it() {
    let ws = Workspace::new();
    let host = Arc::new(ShellHost::default());
    let policy = RemotePolicy {
        cwd: None,
        shared_workspace: true,
    };
    let bash = Bash::in_backend(&backend(&ws, policy, &host));
    let out = text(run(&bash, "pwd", 10_000).await);
    assert_eq!(out, format!("{}\n[exit code 0]", ws.root().display()));
}

#[tokio::test]
async fn a_timeout_kills_the_remote_command() {
    let ws = Workspace::new();
    let (bash, host, _) = bash(&ws);
    let result = run(&bash, "echo begun; sleep 30", 400).await;
    assert_eq!(
        result.as_ref().unwrap_err().code.as_deref(),
        Some("timeout")
    );
    assert_eq!(
        message(result),
        "begun\n[timed out after 400 ms; process group killed]"
    );
    assert_eq!(host.killed.lock().unwrap().len(), 1);
    assert!(!alive(host.pid(0)), "the command outlived its timeout");
}

#[tokio::test]
async fn a_cancel_kills_the_remote_command() {
    let ws = Workspace::new();
    let (bash, host, _) = bash(&ws);
    let context = ctx("s1");
    let cancel = context.cancel.clone();
    let call = bash.execute(input(json!({"command": "sleep 30"})), context);
    let canceller = async {
        tokio::time::sleep(Duration::from_millis(300)).await;
        cancel.cancel();
    };
    let (result, ()) = tokio::join!(call, canceller);
    assert_eq!(
        result.as_ref().unwrap_err().code.as_deref(),
        Some("aborted")
    );
    assert!(!alive(host.pid(0)), "the command outlived its cancel");
}

#[tokio::test]
async fn a_dropped_call_still_tells_the_host_to_kill() {
    let ws = Workspace::new();
    let (bash, host, _) = bash(&ws);
    let call = bash.execute(input(json!({"command": "sleep 30"})), ctx("s1"));
    assert!(
        tokio::time::timeout(Duration::from_millis(300), call)
            .await
            .is_err()
    );
    for _ in 0..100 {
        if !alive(host.pid(0)) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("the command outlived its dropped call");
}

#[tokio::test]
async fn a_host_that_goes_away_gives_an_interrupted_call_not_a_result() {
    let ws = Workspace::new();
    let (bash, host, _) = bash(&ws);
    let context = ctx("s1");
    let call = bash.execute(input(json!({"command": "echo begun; sleep 30"})), context);
    let drop_host = async {
        tokio::time::sleep(Duration::from_millis(400)).await;
        host.gone.store(true, Ordering::SeqCst);
    };
    let (result, ()) = tokio::join!(call, drop_host);
    let error = result.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("interrupted"));
    assert!(
        error.message.starts_with("begun\n[interrupted"),
        "{}",
        error.message
    );
    for made_up in ["exit code", "killed by signal", "timed out"] {
        assert!(!error.message.contains(made_up), "{}", error.message);
    }
    // The server's own kill got no answer either; the test reaps the command.
    kill_process_group(Pid::from_raw(host.pid(0)).unwrap(), Signal::KILL).unwrap();
}

/// A host that starts commands and then never answers anything.
#[derive(Debug)]
struct Silent;

#[async_trait]
impl RemoteExec for Silent {
    async fn start(&self, _: RemoteCommand) -> Result<(), RemoteError> {
        Ok(())
    }
    async fn read(&self, _: &str, _: Duration) -> Result<RemoteChunk, RemoteError> {
        pending().await
    }
    async fn kill(&self, _: &str) -> Result<RemoteChunk, RemoteError> {
        pending().await
    }
}

fn silent_bash(ws: &Workspace) -> Bash {
    let host: Arc<dyn RemoteExec> = Arc::new(Silent);
    let config = ExecutionConfig::Remote(RemotePolicy::default());
    let backend = ExecutionBackend::open_with_host(ws.fs.clone(), &config, Some(host)).unwrap();
    Bash::in_backend(&backend)
}

#[tokio::test(start_paused = true)]
async fn an_unconfirmed_kill_is_interrupted_not_timed_out() {
    let ws = Workspace::new();
    let error = run(&silent_bash(&ws), "sleep 30", 200).await.unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some("interrupted"),
        "{}",
        error.message
    );
    assert!(!error.message.contains("timed out"), "{}", error.message);
}

#[tokio::test(start_paused = true)]
async fn a_host_that_stops_answering_reads_is_interrupted_before_the_timeout() {
    let ws = Workspace::new();
    let started = tokio::time::Instant::now();
    let error = run(&silent_bash(&ws), "sleep 30", 600_000)
        .await
        .unwrap_err();
    assert_eq!(
        error.code.as_deref(),
        Some("interrupted"),
        "{}",
        error.message
    );
    assert!(
        started.elapsed() < Duration::from_secs(30),
        "{:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn a_start_the_host_refuses_is_a_spawn_failure_with_its_message() {
    #[derive(Debug)]
    struct Refusing;
    #[async_trait]
    impl RemoteExec for Refusing {
        async fn start(&self, _: RemoteCommand) -> Result<(), RemoteError> {
            Err(RemoteError::Rejected("no shells today".into()))
        }
        async fn read(&self, _: &str, _: Duration) -> Result<RemoteChunk, RemoteError> {
            unreachable!("never started")
        }
        async fn kill(&self, _: &str) -> Result<RemoteChunk, RemoteError> {
            unreachable!("never started")
        }
    }
    let ws = Workspace::new();
    let host: Arc<dyn RemoteExec> = Arc::new(Refusing);
    let config = ExecutionConfig::Remote(RemotePolicy::default());
    let backend = ExecutionBackend::open_with_host(ws.fs.clone(), &config, Some(host)).unwrap();
    let bash = Bash::in_backend(&backend);
    let result = run(&bash, "true", 10_000).await;
    assert_eq!(
        result.as_ref().unwrap_err().code.as_deref(),
        Some("spawn_failed")
    );
    assert_eq!(
        message(result),
        "could not start the remote command: no shells today"
    );
}

#[tokio::test]
async fn a_background_command_is_read_and_killed_through_the_host() {
    let ws = Workspace::new();
    let (bash, host, processes) = bash(&ws);
    let started = bash
        .execute(
            input(
                json!({"command": "echo first; sleep 1; echo second >&2; exit 4",
                         "run_in_background": true}),
            ),
            ctx("s1"),
        )
        .await
        .unwrap();
    assert!(
        started.metadata.get("pid").is_none(),
        "a remote pid means nothing here"
    );
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    assert!(host.started()[0].background);
    let output = BashOutput::new(Arc::clone(&processes));
    let read = json!({"process_id": id, "wait_ms": 10_000});
    let ended = output.execute(input(read), ctx("s1")).await;
    assert_eq!(
        ended.unwrap_err().message,
        "first\n[stderr]\nsecond\n[exit code 4]"
    );

    let started = bash
        .execute(
            input(json!({"command": "sleep 30", "run_in_background": true})),
            ctx("s1"),
        )
        .await
        .unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    let kill = BashKill::new(Arc::clone(&processes));
    let killed = kill
        .execute(input(json!({"process_id": id})), ctx("s1"))
        .await;
    assert!(killed.unwrap().value.as_str().unwrap().contains("[killed"));
    assert!(!alive(host.pid(1)), "BashKill left the command running");
}

#[tokio::test]
async fn a_background_command_whose_host_left_reads_as_interrupted() {
    let ws = Workspace::new();
    let (bash, host, processes) = bash(&ws);
    let started = bash
        .execute(
            input(json!({"command": "sleep 30", "run_in_background": true})),
            ctx("s1"),
        )
        .await
        .unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    host.gone.store(true, Ordering::SeqCst);
    let output = BashOutput::new(Arc::clone(&processes));
    let read = json!({"process_id": id, "wait_ms": 10_000});
    let error = output.execute(input(read), ctx("s1")).await.unwrap_err();
    assert_eq!(error.code.as_deref(), Some("interrupted"));
    assert!(error.message.contains("[interrupted"), "{}", error.message);
    kill_process_group(Pid::from_raw(host.pid(0)).unwrap(), Signal::KILL).unwrap();
}

#[tokio::test]
async fn closing_a_session_kills_its_remote_background_commands() {
    let ws = Workspace::new();
    let (bash, host, processes) = bash(&ws);
    let _ = bash
        .execute(
            input(json!({"command": "sleep 30", "run_in_background": true})),
            ctx("s1"),
        )
        .await
        .unwrap();
    processes.close_session(&"s1".into());
    for _ in 0..100 {
        if !alive(host.pid(0)) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("the session's command outlived it");
}

#[test]
fn the_file_tools_need_a_shared_workspace_and_the_policy_is_in_the_description() {
    let ws = Workspace::new();
    let host = Arc::new(ShellHost::default());
    let names = |policy: RemotePolicy| -> (Vec<String>, String) {
        let backend = backend(&ws, policy, &host);
        let tools = workspace_tools(&backend, Arc::default(), Arc::default());
        let bash = tools.iter().find(|tool| tool.name() == "Bash").unwrap();
        let names = tools.iter().map(|tool| tool.name().to_owned()).collect();
        (names, bash.description().to_owned())
    };
    let (alone, base) = names(RemotePolicy::default());
    assert_eq!(alone, ["Bash", "BashOutput", "BashKill"]);
    assert!(base.contains("remote host"), "{base}");
    let shared = RemotePolicy {
        cwd: None,
        shared_workspace: true,
    };
    let (all, shared_text) = names(shared);
    assert_eq!(all.len(), 8);
    let with_cwd = RemotePolicy {
        cwd: Some("/srv".into()),
        shared_workspace: false,
    };
    let texts = [base, shared_text, names(with_cwd).1];
    assert_eq!(texts.len(), 3);
    assert!(texts[0] != texts[1] && texts[1] != texts[2] && texts[0] != texts[2]);
}

#[test]
fn a_remote_backend_without_a_host_does_not_open() {
    let ws = Workspace::new();
    let config = ExecutionConfig::Remote(RemotePolicy::default());
    let error = ExecutionBackend::open(ws.fs.clone(), &config).unwrap_err();
    assert!(matches!(error, ExecutionError::NoHost), "{error}");
    assert!(error.to_string().contains("worker"), "{error}");
}

#[test]
fn the_policy_is_strict_and_a_default_policy_is_just_the_backend() {
    let config: ExecutionConfig = serde_json::from_value(json!({"backend": "remote"})).unwrap();
    assert_eq!(config.kind(), "remote");
    assert_eq!(
        serde_json::to_value(&config).unwrap(),
        json!({"backend": "remote"})
    );
    let full = json!({"backend": "remote", "cwd": "/srv", "shared_workspace": true});
    let config: ExecutionConfig = serde_json::from_value(full.clone()).unwrap();
    assert_eq!(serde_json::to_value(&config).unwrap(), full);
    let typo = json!({"backend": "remote", "shared_workspce": true});
    assert!(serde_json::from_value::<ExecutionConfig>(typo).is_err());
}

#[test]
fn the_wire_forms_are_the_documented_ones() {
    let chunk = RemoteChunk {
        stdout: "a".into(),
        stderr: String::new(),
        exit: Some(Exit::Signal(9)),
    };
    let wire = serde_json::to_value(&chunk).unwrap();
    assert_eq!(wire, json!({"stdout": "a", "exit": {"signal": 9}}));
    let empty: RemoteChunk = serde_json::from_value(json!({})).unwrap();
    assert_eq!(empty, RemoteChunk::default());
}
