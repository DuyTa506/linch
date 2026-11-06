//! The virtual filesystem an `AgentConfig` turns on: the four tools and
//! their system text, offloaded results read back through `read_file`,
//! files dropped with their session, and a failed open that takes back
//! what it added.

use std::sync::Arc;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::tools::vfs::PROMPT;
use linch::{
    AgentConfig, Event, FilesystemConfig, Hooks, OpenError, OpenedAgent, PgPools, RunOptions,
    ToolRegistry,
};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";
const VFS_TOOLS: [&str; 4] = ["ls", "read_file", "write_file", "edit_file"];

fn call(id: &str, tool: &str, input: Value) -> Value {
    json!(TurnBuilder::new(MODEL).tool_call(id, tool, &input).build())
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

/// A scripted config allowing writes, with `extra` fields on top.
fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL, "system_prompt": "be brief"},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": "accept_edits", "project_root": "/"},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

/// One tool call's end: its tool, result text and error flag.
struct End {
    tool: String,
    text: String,
    is_error: bool,
}

/// Run `prompt` in `session`; the run's tools and each tool call's end.
async fn run(opened: &OpenedAgent, session: &str) -> (Vec<String>, Vec<End>) {
    let mut stream = opened.agent().run("go", RunOptions::new(session));
    let (mut tools, mut ends) = (Vec::new(), Vec::new());
    while let Some(item) = stream.next().await {
        match item.unwrap().event {
            Event::Init(init) => tools = init.tools,
            Event::ToolCallEnd(end) => ends.push(End {
                tool: end.tool_name,
                text: serde_json::to_value(&end.result).unwrap()["content"][0]["text"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                is_error: end.result.is_error,
            }),
            _ => {}
        }
    }
    (tools, ends)
}

#[tokio::test]
async fn the_filesystem_is_off_unless_the_config_turns_it_on() {
    let off = config(vec![text("hi")], json!({}));
    assert!(off.filesystem.is_none());
    assert!(
        serde_json::to_value(&off)
            .unwrap()
            .get("filesystem")
            .is_none()
    );
    let opened = off.open().await.unwrap();
    assert_eq!(opened.agent().spec().system_prompt, "be brief");
    assert!(run(&opened, "s1").await.0.is_empty());

    let on = config(vec![text("hi")], json!({"filesystem": {}}));
    assert_eq!(on.filesystem, Some(FilesystemConfig::default()));
    let echoed = serde_json::to_value(&on).unwrap();
    assert_eq!(echoed["filesystem"], json!({}));
    let opened = on.open().await.unwrap();
    let prompt = &opened.agent().spec().system_prompt;
    assert_eq!(*prompt, format!("be brief\n\n{PROMPT}"));
    assert_eq!(run(&opened, "s1").await.0, VFS_TOOLS);

    let tuned = config(
        vec![],
        json!({"filesystem": {"offload_threshold_tokens": 500}}),
    );
    let threshold = tuned.filesystem.and_then(|fs| fs.offload_threshold_tokens);
    assert_eq!(threshold, Some(500));
    let mut unknown = serde_json::to_value(config(vec![], json!({}))).unwrap();
    unknown["filesystem"] = json!({"threshold": 1});
    assert!(serde_json::from_value::<AgentConfig>(unknown).is_err());
}

#[tokio::test]
async fn an_offloaded_result_reads_back_through_read_file() {
    let dir = tempfile::tempdir().unwrap();
    let body: Vec<String> = (1..=200).map(|n| format!("line {n}")).collect();
    std::fs::write(dir.path().join("big.txt"), body.join("\n")).unwrap();
    let turns = vec![
        call("call_1", "Read", json!({"file_path": "big.txt"})),
        call(
            "call_2",
            "read_file",
            json!({"path": "/offload/Read_call_1.txt", "offset": 150, "limit": 1}),
        ),
        text("Read it."),
    ];
    let extra = json!({
        "workspace": dir.path(),
        "filesystem": {"offload_threshold_tokens": 10},
    });
    let opened = config(turns, extra).open().await.unwrap();
    let (_, ends) = run(&opened, "s1").await;
    let (read, back) = (&ends[0], &ends[1]);
    assert_eq!((read.tool.as_str(), read.is_error), ("Read", false));
    assert!(
        read.text
            .contains("offloaded to '/offload/Read_call_1.txt'"),
        "{}",
        read.text
    );
    assert!(!read.text.contains("line 150"), "{}", read.text);
    assert_eq!((back.tool.as_str(), back.is_error), ("read_file", false));
    assert!(back.text.contains("line 150"), "{}", back.text);
}

#[tokio::test]
async fn each_session_has_its_own_files_until_it_closes() {
    let read = |id: &str| call(id, "read_file", json!({"path": "/notes.md"}));
    let turns = vec![
        call(
            "call_1",
            "write_file",
            json!({"path": "/notes.md", "content": "alpha"}),
        ),
        text("Wrote."),
        read("call_2"),
        text("Other session."),
        read("call_3"),
        text("Same session."),
        read("call_4"),
        text("After close."),
    ];
    let opened = config(turns, json!({"filesystem": {}}))
        .open()
        .await
        .unwrap();
    let ended = |ends: Vec<End>| (ends[0].text.clone(), ends[0].is_error);
    assert!(!ended(run(&opened, "s1").await.1).1);
    let other = ended(run(&opened, "s2").await.1);
    assert!(other.1 && other.0.contains("File not found"), "{other:?}");
    let same = ended(run(&opened, "s1").await.1);
    assert!(!same.1 && same.0.contains("alpha"), "{same:?}");
    opened.agent().close_session(&"s1".into());
    let closed = ended(run(&opened, "s1").await.1);
    assert!(
        closed.1 && closed.0.contains("File not found"),
        "{closed:?}"
    );
}

#[tokio::test]
async fn a_failed_open_takes_back_its_tools_and_hook() {
    let extra = json!({
        "filesystem": {"offload_threshold_tokens": 10},
        "mcp_servers": [{"name": "missing", "transport": {
            "type": "stdio", "command": "/nonexistent/linch-p10a-server"}}],
    });
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    let opened = (config(vec![], extra))
        .open_with(tools.clone(), hooks.clone(), &PgPools::new())
        .await;
    assert!(matches!(opened, Err(OpenError::Mcp(_))));
    assert!(tools.snapshot().definitions().is_empty());
    assert!(hooks.post_tool_use.is_empty());
}
