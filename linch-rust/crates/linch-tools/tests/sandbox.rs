//! The `sandbox` execution backend: what it confines, tested by running
//! commands that try to get out. Each test skips (with a note on stderr)
//! where the kernel cannot enforce the sandbox, since opening the backend
//! fails closed there.
//!
//! Resource tests stay small and bounded: the machine may be shared.

mod common;

use std::net::TcpListener;
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use common::{Workspace, call, ctx, input, text};
use linch_core::{SessionId, Tool, ToolResult};
use linch_tools::{
    Bash, BashOutput, ExecutionBackend, ExecutionConfig, ExecutionError, Processes, SandboxPolicy,
};
use serde_json::json;

/// The backend over `ws`, or `None` (noted) when this kernel cannot
/// enforce it.
fn sandbox(ws: &Workspace, policy: SandboxPolicy) -> Option<ExecutionBackend> {
    match ExecutionBackend::open(ws.fs.clone(), &ExecutionConfig::Sandbox(policy)) {
        Ok(backend) => Some(backend),
        Err(error @ (ExecutionError::Landlock(_) | ExecutionError::Unsupported(_))) => {
            eprintln!("skipped: {error}");
            None
        }
        Err(error) => panic!("{error}"),
    }
}

async fn run_in(tool: &Bash, session: &str, command: &str) -> ToolResult {
    let value = json!({"command": command, "timeout_ms": 20_000});
    tool.execute(input(value), ctx(session)).await
}

async fn run(tool: &Bash, command: &str) -> ToolResult {
    run_in(tool, "s1", command).await
}

/// Text of a result, success or error.
fn any(result: ToolResult) -> String {
    match result {
        Ok(output) => output.value.as_str().unwrap().to_owned(),
        Err(error) => error.message,
    }
}

fn python() -> bool {
    Path::new("/usr/bin/python3").is_file()
}

#[tokio::test]
async fn a_command_reads_and_writes_only_the_workspace_and_its_home() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    ws.put("inside.txt", "visible");
    let secret = ws.outside().join("secret.txt");
    std::fs::write(&secret, "hidden").unwrap();

    assert_eq!(
        text(run(&tool, "cat inside.txt").await),
        "visible\n[exit code 0]"
    );
    let out = text(run(&tool, "echo made > new.txt && cat new.txt").await);
    assert_eq!(out, "made\n[exit code 0]");
    assert_eq!(ws.get("new.txt"), "made\n");
    // System programs and configuration still work.
    let out = text(
        run(
            &tool,
            "ls /usr/bin/env >/dev/null && head -c 0 /etc/passwd && echo ok",
        )
        .await,
    );
    assert_eq!(out, "ok\n[exit code 0]");

    let read = any(run(&tool, &format!("cat {}", secret.display())).await);
    assert!(read.contains("Permission denied"), "{read}");
    let write = any(run(&tool, &format!("echo x > {}/new", ws.outside().display())).await);
    assert!(write.contains("Permission denied"), "{write}");
    assert!(!ws.outside().join("new").exists());
    // The server user's home, where keys and config live, is closed.
    if let Ok(home) = std::env::var("HOME") {
        let listed = any(run(&tool, &format!("ls {home}")).await);
        assert!(listed.contains("Permission denied"), "{listed}");
    }
    // `/tmp` outside the private one is closed too.
    let tmp = any(run(&tool, "ls /tmp").await);
    assert!(tmp.contains("Permission denied"), "{tmp}");
    // The server's controlling terminal is closed too.
    let tty = any(run(&tool, ": > /dev/tty").await);
    assert!(tty.contains("Permission denied"), "{tty}");
}

#[tokio::test]
async fn no_host_secret_reaches_the_command() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let out = text(run(&tool, "env").await);
    let names: Vec<&str> = (out.lines())
        .filter_map(|line| line.split_once('=').map(|(name, _)| name))
        .collect();
    let allowed = [
        "PATH", "HOME", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "USER", "SHELL", "TMPDIR", "PWD",
    ];
    assert!(names.iter().all(|name| allowed.contains(name)), "{names:?}");
    // HOME and TMPDIR are the private home, not the server's.
    let home = text(run(&tool, "echo $HOME; echo $TMPDIR").await);
    let mut lines = home.lines();
    let (home, tmpdir) = (lines.next().unwrap(), lines.next().unwrap());
    assert_eq!(home, tmpdir);
    assert_ne!(Some(home.to_owned()), std::env::var("HOME").ok());
    // The server's environment, where provider keys are, cannot be read
    // through /proc either: `$PPID` is this test process.
    let environ = any(run(&tool, "cat /proc/$PPID/environ").await);
    assert!(environ.contains("Permission denied"), "{environ}");
    let own = text(run(&tool, "tr '\\0' '\\n' < /proc/$$/environ | grep -c HOME").await);
    assert_eq!(own, "1\n[exit code 0]");
}

#[tokio::test]
async fn a_command_cannot_signal_the_server() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let out = any(run(&tool, "kill -0 $PPID").await);
    assert!(out.contains("Operation not permitted"), "{out}");
    // Its own processes it can.
    let own = text(run(&tool, "sleep 5 & kill $! && echo stopped").await);
    assert_eq!(own, "stopped\n[exit code 0]");
}

#[tokio::test]
async fn the_network_is_off_unless_the_policy_turns_it_on() {
    if !python() {
        eprintln!("skipped: no /usr/bin/python3");
        return;
    }
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let connect = format!(
        "python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 5); print('connected')\""
    );
    let udp = "python3 -c \"import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM); print('udp')\"";

    let ws = Workspace::new();
    let Some(off) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&off);
    let out = any(run(&tool, &connect).await);
    assert!(out.contains("PermissionError"), "{out}");
    let out = any(run(&tool, udp).await);
    assert!(out.contains("PermissionError"), "{out}");

    let on = SandboxPolicy {
        network: true,
        ..SandboxPolicy::default()
    };
    let Some(on) = sandbox(&ws, on) else {
        return;
    };
    let tool = Bash::in_backend(&on);
    assert_eq!(text(run(&tool, &connect).await), "connected\n[exit code 0]");
    assert_eq!(text(run(&tool, udp).await), "udp\n[exit code 0]");
}

/// A Unix socket of the server's user (a session bus, a Docker socket)
/// would start things outside the sandbox: it is refused with the network
/// on too.
#[tokio::test]
async fn unix_sockets_are_refused() {
    if !python() {
        eprintln!("skipped: no /usr/bin/python3");
        return;
    }
    let ws = Workspace::new();
    let path = ws.outside().join("bus.sock");
    let _listener = UnixListener::bind(&path).unwrap();
    let policy = SandboxPolicy {
        network: true,
        ..SandboxPolicy::default()
    };
    let Some(backend) = sandbox(&ws, policy) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let connect = format!(
        "python3 -c \"import socket; s = socket.socket(socket.AF_UNIX); s.connect('{}'); print('connected')\"",
        path.display()
    );
    let out = any(run(&tool, &connect).await);
    assert!(out.contains("PermissionError"), "{out}");
}

#[tokio::test]
async fn a_process_storm_hits_the_process_limit() {
    let ws = Workspace::new();
    let policy = SandboxPolicy {
        max_processes: 16,
        ..SandboxPolicy::default()
    };
    let Some(backend) = sandbox(&ws, policy) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    // Bounded: 64 sleepers at most, killed with the group at the end.
    let storm = "i=0; while [ $i -lt 64 ]; do sleep 3 & i=$((i+1)); done; echo spawned";
    let out = any(run(&tool, storm).await);
    assert!(out.contains("Cannot fork"), "{out}");
}

#[tokio::test]
async fn a_memory_hog_hits_the_memory_limit() {
    if !python() {
        eprintln!("skipped: no /usr/bin/python3");
        return;
    }
    let ws = Workspace::new();
    let policy = SandboxPolicy {
        memory_bytes: 256 * 1024 * 1024,
        ..SandboxPolicy::default()
    };
    let Some(backend) = sandbox(&ws, policy) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let hog = "python3 -c \"b = bytearray(512 * 1024 * 1024); print('allocated')\"";
    let out = any(run(&tool, hog).await);
    assert!(out.contains("MemoryError"), "{out}");
    let small = "python3 -c \"b = bytearray(16 * 1024 * 1024); print('allocated')\"";
    assert_eq!(text(run(&tool, small).await), "allocated\n[exit code 0]");
}

#[tokio::test]
async fn a_busy_loop_hits_the_cpu_limit_and_a_big_file_the_size_limit() {
    let ws = Workspace::new();
    let policy = SandboxPolicy {
        cpu_seconds: 1,
        file_size_bytes: 1024 * 1024,
        ..SandboxPolicy::default()
    };
    let Some(backend) = sandbox(&ws, policy) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let busy = run(&tool, "while :; do :; done").await.unwrap_err();
    // SIGXCPU.
    assert_eq!(
        busy.details,
        Some(json!({"signal": 24})),
        "{}",
        busy.message
    );
    let big = run(&tool, "head -c 2000000 /dev/zero > big")
        .await
        .unwrap_err();
    assert_eq!(big.code.as_deref(), Some("exit_status"), "{}", big.message);
    let size = std::fs::metadata(ws.root().join("big")).unwrap().len();
    assert!(size <= 1024 * 1024, "{size}");
}

#[tokio::test]
async fn each_session_has_a_private_home_dropped_when_it_closes() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    let home = text(run_in(&tool, "a", "echo kept > $HOME/note; echo $HOME").await);
    let home = home.lines().next().unwrap().to_owned();
    assert_eq!(
        text(run_in(&tool, "a", "cat $HOME/note").await),
        "kept\n[exit code 0]"
    );
    let other = any(run_in(&tool, "b", &format!("cat {home}/note")).await);
    assert!(other.contains("Permission denied"), "{other}");
    backend.close_session(&SessionId::from("a"));
    for _ in 0..50 {
        if !Path::new(&home).exists() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(!Path::new(&home).exists(), "the home outlived its session");
}

#[tokio::test]
async fn a_background_command_is_sandboxed_and_dies_with_its_session() {
    let ws = Workspace::new();
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let processes = Arc::new(Processes::new());
    let tool = Bash::in_backend(&backend).with_processes(Arc::clone(&processes));
    let secret = ws.outside().join("secret.txt");
    std::fs::write(&secret, "hidden").unwrap();
    let command = format!("cat {} ; echo started; sleep 30", secret.display());
    let value = json!({"command": command, "run_in_background": true});
    let started = tool.execute(input(value), ctx("s1")).await.unwrap();
    let id = started.metadata["process_id"].as_str().unwrap().to_owned();
    let pid = started.metadata["pid"].as_u64().unwrap();
    let output = BashOutput::new(Arc::clone(&processes));
    let mut seen = String::new();
    for _ in 0..50 {
        let read = json!({"process_id": id, "wait_ms": 100});
        seen.push_str(&text(output.execute(input(read), ctx("s1")).await));
        if seen.contains("started") {
            break;
        }
    }
    assert!(seen.contains("Permission denied"), "{seen}");
    assert_eq!(processes.running(&SessionId::from("s1")), 1);
    processes.close_session(&SessionId::from("s1"));
    let proc = Path::new("/proc").join(pid.to_string());
    for _ in 0..100 {
        if !proc.exists() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(
        !proc.exists(),
        "the background command outlived its session"
    );
}

#[tokio::test]
async fn the_description_states_the_confinement() {
    let ws = Workspace::new();
    let local = Bash::new(ws.root());
    let Some(backend) = sandbox(&ws, SandboxPolicy::default()) else {
        return;
    };
    let tool = Bash::in_backend(&backend);
    assert!(tool.description().starts_with(local.description()));
    let extra = &tool.description()[local.description().len()..];
    assert!(extra.contains("have no network"), "{extra}");
    assert!(extra.contains("2048 MiB of memory"), "{extra}");
    assert!(backend.sandboxed());
    let _ = call;
}

#[test]
fn a_policy_reads_strictly_with_defaults() {
    let config: ExecutionConfig = serde_json::from_value(json!({"backend": "sandbox"})).unwrap();
    assert_eq!(config, ExecutionConfig::Sandbox(SandboxPolicy::default()));
    let local: ExecutionConfig = serde_json::from_value(json!({"backend": "local"})).unwrap();
    assert!(local.is_local());
    let extra = json!({"backend": "local", "network": false});
    assert!(serde_json::from_value::<ExecutionConfig>(extra).is_err());
    let typo = json!({"backend": "sandbox", "netwrok": true});
    assert!(serde_json::from_value::<ExecutionConfig>(typo).is_err());
    let docker: ExecutionConfig =
        serde_json::from_value(json!({"backend": "docker", "network": "bridge"})).unwrap();
    assert_eq!(docker.kind(), "docker");
    let relative = SandboxPolicy {
        read_paths: vec!["data".into()],
        ..SandboxPolicy::default()
    };
    let ws = Workspace::new();
    let invalid = ExecutionBackend::open(ws.fs.clone(), &ExecutionConfig::Sandbox(relative));
    assert!(
        matches!(invalid, Err(ExecutionError::Invalid { .. })),
        "{invalid:?}"
    );
}
