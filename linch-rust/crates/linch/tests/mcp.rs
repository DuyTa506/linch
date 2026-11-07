//! MCP servers declared in `AgentConfig`: strict decoding, a stdio server
//! connected at open whose tools a run calls, and a clean disconnect at
//! close. The server is the `mcp_stdio_server` example.

use std::path::PathBuf;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, McpConfig, OpenError, RunOptions};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

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

fn server_program() -> String {
    let program = example("mcp_stdio_server");
    assert!(
        program.exists(),
        "{} was not built: run `cargo test -p linch` without a target filter",
        program.display()
    );
    program.to_string_lossy().into_owned()
}

fn config(turns: Value, mcp_servers: Value) -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "mcp_servers": mcp_servers,
    }))
    .unwrap()
}

#[test]
fn an_mcp_config_reads_strictly_and_hides_secret_values() {
    let stdio: McpConfig = serde_json::from_value(json!({
        "name": "fs",
        "transport": {"type": "stdio", "command": "server", "env": {"TOKEN": "hunter2"}},
    }))
    .unwrap();
    assert_eq!(stdio.call_timeout_ms, 300_000);
    assert!(!stdio.trust_annotations);
    assert_eq!(stdio.reconnect.attempts, 5);
    assert_eq!(stdio.reconnect.attempt_timeout_ms, 30_000);
    assert!(!format!("{stdio:?}").contains("hunter2"));

    let http: McpConfig = serde_json::from_value(json!({
        "name": "web",
        "transport": {"type": "http", "url": "https://example.com/mcp",
                      "headers": {"Authorization": "Bearer s3cret"}},
        "reconnect": {"attempts": 0},
    }))
    .unwrap();
    assert_eq!(http.reconnect.attempts, 0);
    assert_eq!(http.reconnect.max_delay_ms, 30_000);
    let shown = format!("{http:?}");
    assert!(shown.contains("Authorization") && !shown.contains("s3cret"));

    for rejected in [
        json!({"name": "a", "transport": {"type": "stdio", "command": "x"}, "x": 1}),
        json!({"name": "a", "transport": {"type": "stdio", "command": "x", "url": "u"}}),
        json!({"name": "a", "transport": {"type": "custom"}}),
        json!({"name": "a", "transport": {"type": "http", "url": "u"},
               "reconnect": {"retries": 1}}),
    ] {
        let text = rejected.to_string();
        assert!(
            serde_json::from_value::<McpConfig>(rejected).is_err(),
            "{text}"
        );
    }
}

#[tokio::test]
async fn an_mcp_server_from_config_serves_a_run_and_closes() {
    let program = server_program();
    let calls = TurnBuilder::new(MODEL)
        .tool_call("call_1", "mcp__stdio__echo", &json!({"text": "over stdio"}))
        .tool_call("call_2", "mcp__stdio__pid", &json!({}))
        .build();
    let done = TurnBuilder::new(MODEL).text("Done.").build();
    let servers = json!([{"name": "stdio",
                          "transport": {"type": "stdio", "command": program}}]);
    let without = config(json!([]), json!([])).open().await.unwrap();
    let opened = config(json!([calls, done]), servers).open().await.unwrap();
    assert_ne!(opened.agent().contract(), without.agent().contract());
    without.close().await;
    let names = opened.mcp_servers()[0].tools();
    assert!(names.contains(&"mcp__stdio__echo".to_owned()), "{names:?}");

    let mut run = opened.agent().run("Go.", RunOptions::new("s1"));
    let mut ends = Vec::new();
    while let Some(envelope) = run.next().await {
        if let Event::ToolCallEnd(end) = envelope.unwrap().event {
            ends.push(serde_json::to_value(&end.result).unwrap());
        }
    }
    drop(run);
    assert_eq!(ends.len(), 2);
    assert!(ends[0].to_string().contains("over stdio"), "{}", ends[0]);
    let pid: u32 = ends[1]["content"][0]["text"]
        .as_str()
        .expect("pid text")
        .parse()
        .unwrap();
    assert_ne!(pid, std::process::id(), "the server is a child process");

    opened.close().await;
    #[cfg(target_os = "linux")]
    assert!(
        !std::path::Path::new(&format!("/proc/{pid}")).exists(),
        "the MCP server process {pid} is still running"
    );
}

#[tokio::test]
async fn an_mcp_server_that_cannot_start_fails_the_open() {
    let servers = json!([
        {"name": "ok", "transport": {"type": "stdio", "command": server_program()}},
        {"name": "missing", "transport": {"type": "stdio",
                                          "command": "/nonexistent/linch-mcp-server"}},
    ]);
    let error = config(json!([]), servers).open().await.unwrap_err();
    assert!(matches!(error, OpenError::Mcp(_)), "{error}");
    assert!(error.to_string().contains("missing"), "{error}");

    let bad_name = json!([{"name": "a__b", "transport": {"type": "http", "url": "u"}}]);
    let error = config(json!([]), bad_name).open().await.unwrap_err();
    assert!(matches!(error, OpenError::Mcp(_)), "{error}");
}
