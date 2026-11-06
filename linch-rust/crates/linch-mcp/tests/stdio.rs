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

#[tokio::test]
async fn stdio_round_trip_and_clean_exit() {
    let program = example("stdio_server");
    assert!(
        program.exists(),
        "{} was not built: run `cargo test -p linch-mcp` without a target filter, \
         or `cargo build -p linch-mcp --example stdio_server` first",
        program.display()
    );
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
