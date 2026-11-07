//! An MCP server from the config over RPC: its tool serves a run, and the
//! server process stops when the client's connection ends. The MCP server
//! is the `mcp_server` example.

mod common;

use std::path::PathBuf;
use std::time::Duration;

use common::*;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::Client;
use serde_json::json;

/// `cargo test` builds the examples next to the test binaries' `deps`
/// directory.
fn example(name: &str) -> PathBuf {
    let mut dir = std::env::current_exe().expect("test binary path");
    dir.pop();
    if dir.ends_with("deps") {
        dir.pop();
    }
    dir.join("examples").join(name)
}

#[tokio::test]
async fn an_mcp_server_serves_a_run_and_stops_with_the_connection() {
    let program = example("mcp_server");
    assert!(
        program.exists(),
        "{} was not built: run `cargo test -p linch-rpc` without a target filter",
        program.display()
    );
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let serving = tokio::spawn(linch_rpc::serve(server_reader, server_writer));
    let (reader, writer) = tokio::io::split(ours);
    let mut rpc = Rpc::connected(Client::connect(reader, writer));

    let pid_turn = TurnBuilder::new(MODEL)
        .tool_call("call_1", "mcp__stdio__pid", &json!({}))
        .build();
    let servers = json!({"mcp_servers": [{"name": "stdio", "transport": {
        "type": "stdio", "command": program}}]});
    let config = hosted(
        vec![pid_turn, text_turn("Done.")],
        "skip_dangerous",
        servers,
    );
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "Which process?").await;
    let ended = rpc.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    let result = tool_results(&ended).remove(0);
    let pid: u32 = result["content"][0]["text"]
        .as_str()
        .expect("pid text")
        .parse()
        .unwrap();
    assert_ne!(pid, std::process::id(), "the server is a child process");

    drop(rpc);
    let stopped = tokio::time::timeout(Duration::from_secs(10), serving).await;
    stopped.expect("serve stops").unwrap().unwrap();
    #[cfg(target_os = "linux")]
    assert!(
        !std::path::Path::new(&format!("/proc/{pid}")).exists(),
        "the MCP server process {pid} is still running"
    );
}
