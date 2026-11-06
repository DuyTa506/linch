//! `agent.close`: it stops the agent's MCP server processes and frees the
//! handle, and it is refused while the server still drives a run of the
//! agent. The MCP server is the `mcp_server` example.

mod common;

use std::future::pending;
use std::path::PathBuf;

use common::*;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::method;
use linch_rpc::{ClientError, ErrorObject, code};
use serde_json::{Value, json};
use tokio::sync::mpsc;

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

fn code_of(error: ClientError) -> i64 {
    match error {
        ClientError::Rpc(error) => error.code,
        other => panic!("expected an error object, got {other:?}"),
    }
}

#[tokio::test]
async fn agent_close_stops_mcp_servers_and_frees_the_handle() {
    let program = example("mcp_server");
    assert!(program.exists(), "{} was not built", program.display());
    let mut rpc = Rpc::new();
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
        .unwrap()
        .parse()
        .unwrap();

    let close = json!({"agent": agent});
    let closed = rpc.call(method::AGENT_CLOSE, close.clone()).await.unwrap();
    assert_eq!(closed, json!({}));
    #[cfg(target_os = "linux")]
    assert!(
        !std::path::Path::new(&format!("/proc/{pid}")).exists(),
        "the MCP server process {pid} is still running"
    );
    let again = rpc.call(method::AGENT_CLOSE, close).await.unwrap_err();
    assert_eq!(code_of(again), code::AGENT_NOT_FOUND);
    let start = json!({"agent": agent, "session_id": "s2", "prompt": "p"});
    let gone = rpc.call(method::RUN_START, start).await.unwrap_err();
    assert_eq!(code_of(gone), code::AGENT_NOT_FOUND);

    // The connection keeps serving other agents.
    let other = rpc
        .open(scripted(vec![text_turn("still here")], None, ""))
        .await;
    let run_id = rpc.start(&other, "s1", "go").await;
    assert!(rpc.until_end(&run_id).await.error.is_none());
}

#[tokio::test]
async fn agent_close_is_refused_while_a_run_of_it_is_driven() {
    let (arrived, mut calls) = mpsc::unbounded_channel();
    let mut rpc = Rpc::with_handler(handler(move |_, _, _| {
        let _ = arrived.send(());
        pending::<Result<Value, ErrorObject>>()
    }));
    let turns = vec![
        tool_turn("call_1", "lookup", json!({"q": "x"})),
        text_turn("unused"),
    ];
    let tools = json!({"tools": [host_tool("lookup", "read")]});
    let agent = rpc.open(hosted(turns, "default", tools)).await;
    let run_id = rpc.start(&agent, "s1", "Look it up.").await;
    calls.recv().await.expect("the tool call arrives");

    let close = json!({"agent": agent});
    let busy = rpc
        .call(method::AGENT_CLOSE, close.clone())
        .await
        .unwrap_err();
    assert_eq!(code_of(busy), code::RUN_BUSY);

    let cancel = json!({"run_id": run_id});
    rpc.call(method::RUN_CANCEL, cancel).await.unwrap();
    rpc.until_end(&run_id).await;
    let closed = rpc.call(method::AGENT_CLOSE, close).await.unwrap();
    assert_eq!(closed, json!({}));
}
