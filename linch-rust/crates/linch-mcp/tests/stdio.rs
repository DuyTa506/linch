//! One round trip through a stdio child process: the `stdio_server` example.

mod common;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{call, text_input};
use linch_core::{JsonObject, ToolRegistry};
use linch_mcp::{McpServer, McpServerConfig, McpTransport, ServerStatus, codes};
use serde_json::json;

/// `cargo test` builds the examples next to the test binaries'
/// `deps` directory.
fn example(name: &str) -> PathBuf {
    let mut dir = std::env::current_exe().expect("test binary path");
    dir.pop();
    if dir.ends_with("deps") {
        dir.pop();
    }
    dir.join("examples").join(name)
}

/// The example server, which must have been built.
fn stdio_server() -> PathBuf {
    let program = example("stdio_server");
    assert!(
        program.exists(),
        "{} was not built: run `cargo test -p linch-mcp` without a target filter, \
         or `cargo build -p linch-mcp --example stdio_server` first",
        program.display()
    );
    program
}

#[tokio::test]
async fn stdio_round_trip_and_clean_exit() {
    let program = stdio_server();
    let registry = Arc::new(ToolRegistry::new());
    let config = McpServerConfig::new(
        "stdio",
        McpTransport::stdio(program.to_string_lossy(), Vec::new()),
    );
    let server = McpServer::connect(config, Arc::clone(&registry))
        .await
        .expect("connect over stdio");
    assert!(server.tools().contains(&"mcp__stdio__echo".to_owned()));

    let echoed = call(&registry, "mcp__stdio__echo", text_input("over stdio"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("over stdio"));
    let failed = call(&registry, "mcp__stdio__fail", JsonObject::new())
        .await
        .unwrap_err();
    assert_eq!(failed.code.as_deref(), Some(codes::TOOL_ERROR));

    let pid = call(&registry, "mcp__stdio__pid", JsonObject::new())
        .await
        .unwrap();
    let pid: u32 = pid.value.as_str().unwrap().parse().unwrap();
    assert_ne!(pid, std::process::id(), "the server is a child process");

    let mut status = server.watch_status();
    let started = Instant::now();
    server.disconnect().await;
    // The subscription is cancelled before stdin closes, so the server exits
    // on EOF instead of waiting for it and being sent SIGTERM after 3 s.
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "disconnect took {:?}",
        started.elapsed()
    );
    assert_eq!(*status.borrow_and_update(), ServerStatus::Closed);
    assert!(registry.snapshot().definitions().is_empty());
    // Disconnect waits for the child to exit and reaps it.
    #[cfg(target_os = "linux")]
    assert!(
        !std::path::Path::new(&format!("/proc/{pid}")).exists(),
        "the server process {pid} is still running"
    );
}

/// Send `signal` (`STOP`, `KILL`) to process `pid`.
fn signal(signal: &str, pid: u32) {
    let status = std::process::Command::new("kill")
        .args([format!("-{signal}"), pid.to_string()])
        .status()
        .expect("run kill");
    assert!(status.success(), "kill -{signal} {pid} failed");
}

#[tokio::test]
async fn disconnect_kills_a_server_that_stopped_reading() {
    let registry = Arc::new(ToolRegistry::new());
    let mut config = McpServerConfig::new(
        "stalled",
        McpTransport::stdio(stdio_server().to_string_lossy(), Vec::new()),
    );
    config.call_timeout = Duration::from_millis(500);
    let server = McpServer::connect(config, Arc::clone(&registry))
        .await
        .expect("connect over stdio");
    let pid = call(&registry, "mcp__stalled__pid", JsonObject::new())
        .await
        .unwrap();
    let pid: u32 = pid.value.as_str().unwrap().parse().unwrap();

    // A stopped server reads nothing. The request is larger than the pipe,
    // so its write, and the close after it, wait for a reader.
    signal("STOP", pid);
    let big = text_input(&"x".repeat(256 * 1024));
    let timed_out = call(&registry, "mcp__stalled__echo", big)
        .await
        .unwrap_err();
    assert_eq!(timed_out.code.as_deref(), Some(codes::TIMEOUT));

    let closed = tokio::time::timeout(Duration::from_secs(60), server.disconnect()).await;
    if closed.is_err() {
        signal("KILL", pid);
    }
    assert!(
        closed.is_ok(),
        "disconnect hung on a server that stopped reading"
    );
    #[cfg(target_os = "linux")]
    {
        let deadline = Instant::now() + Duration::from_secs(30);
        while std::path::Path::new(&format!("/proc/{pid}")).exists() {
            assert!(
                Instant::now() < deadline,
                "the stopped server {pid} survived"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
}

#[tokio::test]
async fn disconnect_kills_what_the_server_started() {
    let marker = std::env::temp_dir().join(format!("linch-mcp-orphan-{}", std::process::id()));
    let _ = std::fs::remove_file(&marker);
    // Like `npx` or `uvx`: a launcher leaves a child behind, here one that
    // would write the marker after 2 s, then becomes the server.
    let script = r#"(sleep 2; touch "$0") & exec "$1""#;
    let args = vec![
        "-c".to_owned(),
        script.to_owned(),
        marker.to_string_lossy().into_owned(),
        stdio_server().to_string_lossy().into_owned(),
    ];
    let registry = Arc::new(ToolRegistry::new());
    let config = McpServerConfig::new("launched", McpTransport::stdio("/bin/sh", args));
    let server = McpServer::connect(config, Arc::clone(&registry))
        .await
        .expect("connect through sh");
    let echoed = call(&registry, "mcp__launched__echo", text_input("up"))
        .await
        .unwrap();
    assert_eq!(echoed.value, json!("up"));
    server.disconnect().await;

    tokio::time::sleep(Duration::from_secs(3)).await;
    let survived = marker.exists();
    let _ = std::fs::remove_file(&marker);
    assert!(!survived, "the server's child outlived the disconnect");
}

#[tokio::test]
async fn a_server_that_closed_its_stdin_is_reconnected() {
    // The launcher passes the client's lines to the server until one says
    // `close-stdin`. Then it closes the client's pipe, passes that line on
    // and holds the server's input open, so the server keeps running and
    // answers on stdout, but nothing the client writes is read any more.
    let script = r#"exec 3<&0 0</dev/null
{
    while IFS= read -r line <&3; do
        case $line in
            *close-stdin*) exec 3<&-; printf '%s\n' "$line"; exec sleep 120 ;;
        esac
        printf '%s\n' "$line"
    done
} | "$1" 3<&- &
exec 3<&-
wait"#;
    let args = vec![
        "-c".to_owned(),
        script.to_owned(),
        "sh".to_owned(),
        stdio_server().to_string_lossy().into_owned(),
    ];
    let registry = Arc::new(ToolRegistry::new());
    let config = McpServerConfig::new("half", McpTransport::stdio("/bin/sh", args));
    let server = McpServer::connect(config, Arc::clone(&registry))
        .await
        .expect("connect through sh");
    let pid =
        |output: linch_core::ToolOutput| -> u32 { output.value.as_str().unwrap().parse().unwrap() };
    let first = pid(call(&registry, "mcp__half__pid", JsonObject::new())
        .await
        .unwrap());

    let closing = call(&registry, "mcp__half__echo", text_input("close-stdin"))
        .await
        .unwrap();
    assert_eq!(closing.value, json!("close-stdin"));
    let failed = call(&registry, "mcp__half__echo", text_input("unread"))
        .await
        .unwrap_err();
    assert_eq!(failed.code.as_deref(), Some(codes::UNAVAILABLE));

    // The first server is closed and a fresh one started.
    let deadline = Instant::now() + Duration::from_secs(30);
    let second = loop {
        match call(&registry, "mcp__half__pid", JsonObject::new()).await {
            Ok(output) => break pid(output),
            Err(error) => assert!(
                Instant::now() < deadline,
                "never reconnected: {}",
                error.message
            ),
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    assert_ne!(second, first);
    server.disconnect().await;
}
