//! End of input: calls already read are still answered, and an
//! `agent.open` in flight registers its agent so its MCP servers are
//! closed and awaited. The MCP server is the `mcp_server` example; no
//! other test in this binary starts one.

mod common;

use std::path::PathBuf;
use std::time::Duration;

use common::*;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

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

/// Write `lines`, end the input at once, and return every line the server
/// wrote before `serve` returned.
async fn answers_after_eof(lines: &[Value]) -> Vec<Value> {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let serving = tokio::spawn(linch_rpc::serve(server_reader, server_writer));
    let (reader, mut writer) = tokio::io::split(ours);
    for line in lines {
        writer
            .write_all(format!("{line}\n").as_bytes())
            .await
            .unwrap();
    }
    writer.shutdown().await.unwrap();
    let served = tokio::time::timeout(Duration::from_secs(20), serving).await;
    served.expect("serve returns").unwrap().unwrap();
    let mut output = BufReader::new(reader).lines();
    let mut seen = Vec::new();
    while let Some(line) = output.next_line().await.unwrap() {
        seen.push(serde_json::from_str(&line).unwrap());
    }
    seen
}

#[tokio::test]
async fn calls_read_before_the_end_of_input_are_answered() {
    for _ in 0..5 {
        let open = json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open",
                          "params": scripted(vec![text_turn("hi")], None, "")});
        let cancel = json!({"jsonrpc": "2.0", "id": 2, "method": "run.cancel",
                            "params": {"run_id": "r"}});
        let seen = answers_after_eof(&[open, cancel]).await;
        let mut ids: Vec<i64> = seen.iter().filter_map(|m| m["id"].as_i64()).collect();
        ids.sort();
        assert_eq!(ids, [1, 2], "{seen:?}");
        assert!(seen.iter().all(|m| m.get("error").is_none()), "{seen:?}");
    }
}

/// Children of this process running the example MCP server.
#[cfg(target_os = "linux")]
fn mcp_children() -> Vec<String> {
    let me = std::process::id().to_string();
    let mut found = Vec::new();
    for entry in std::fs::read_dir("/proc").unwrap().flatten() {
        let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
            continue;
        };
        // `pid (comm) state ppid ...`; comm may hold spaces, so split after it.
        let Some((head, rest)) = stat.rsplit_once(')') else {
            continue;
        };
        let fields: Vec<&str> = rest.split_whitespace().collect();
        if head.contains("(mcp_server") && fields.get(1) == Some(&me.as_str()) {
            found.push(stat);
        }
    }
    found
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn an_open_in_flight_at_the_end_of_input_leaves_no_mcp_server() {
    let program = example("mcp_server");
    assert!(program.exists(), "{} was not built", program.display());
    let servers = json!({"mcp_servers": [{"name": "stdio", "transport": {
        "type": "stdio", "command": program}}]});
    let open = json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open",
                      "params": hosted(vec![], "default", servers)});
    let seen = answers_after_eof(&[open]).await;
    assert!(seen[0]["result"]["agent"].is_string(), "{seen:?}");
    assert_eq!(mcp_children(), Vec::<String>::new());
}
