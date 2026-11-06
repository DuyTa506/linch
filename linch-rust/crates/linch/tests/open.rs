//! Opening agents from `AgentConfig`: strict decoding, the scripted
//! provider over the workspace tools, and errors that never show secrets.

use std::error::Error;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, BuildError, Event, OpenError, RunOptions, StoreConfig};
use serde_json::{Value, json};

fn config(value: Value) -> AgentConfig {
    serde_json::from_value(value).unwrap()
}

fn scripted(turns: Value, workspace: Option<&str>) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": turns},
    });
    if let Some(root) = workspace {
        value["workspace"] = json!(root);
    }
    config(value)
}

/// The full error text, each source added once.
fn chain(error: &dyn Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(next) = source {
        text = format!("{text} | {next}");
        source = next.source();
    }
    text
}

#[test]
fn a_config_reads_strictly_with_defaults() {
    let minimal = config(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "deepseek"},
    }));
    assert_eq!(minimal.stores, StoreConfig::Memory {});
    assert!(minimal.workspace.is_none() && minimal.permissions.is_none());
    let rejected = [
        json!({"namespace": "t", "spec": {"model": "m"}, "provider": {"id": "deepseek"}, "x": 1}),
        json!({"namespace": "t", "spec": {"model": "m"},
               "provider": {"id": "deepseek", "api_key": "sk-1"}}),
        json!({"namespace": "t", "spec": {"model": "m"}, "provider": {"id": "nope"}}),
        json!({"namespace": "t", "spec": {"model": "m"}, "provider": {"id": "vllm"}}),
        json!({"namespace": "t", "spec": {"model": "m"}, "provider": {"id": "deepseek"},
               "stores": {"type": "memory", "dsn": "x"}}),
    ];
    for value in rejected {
        let text = value.to_string();
        assert!(
            serde_json::from_value::<AgentConfig>(value).is_err(),
            "{text}"
        );
    }
}

#[tokio::test]
async fn a_scripted_agent_reads_a_workspace_file() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("notes.txt"), "alpha\n").unwrap();
    let turns = json!([
        TurnBuilder::new("scripted-model")
            .tool_call("call_1", "Read", &json!({"file_path": "notes.txt"}))
            .build(),
        TurnBuilder::new("scripted-model")
            .text("It says alpha.")
            .build(),
    ]);
    let agent = scripted(turns, dir.path().to_str()).open().await.unwrap();
    let mut stream = agent.run("Read notes.txt", RunOptions::new("s1"));
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.unwrap().event);
    }
    let Event::Init(init) = &events[0] else {
        panic!("first event {:?}", events[0]);
    };
    assert_eq!(init.tools.len(), 6, "{:?}", init.tools);
    let end = events.iter().find_map(|event| match event {
        Event::ToolCallEnd(end) => Some(end.clone()),
        _ => None,
    });
    let end = serde_json::to_string(&end.unwrap()).unwrap();
    assert!(end.contains("alpha"), "{end}");
    match events.last().unwrap() {
        Event::Result(result) => assert_eq!(result.final_text.as_deref(), Some("It says alpha.")),
        other => panic!("last event {other:?}"),
    }
}

#[tokio::test]
async fn a_missing_workspace_or_blank_model_fails_to_open() {
    let missing = scripted(json!([]), Some("/nonexistent/linch-p11"));
    assert!(matches!(missing.open().await, Err(OpenError::Workspace(_))));
    let mut blank = scripted(json!([]), None);
    blank.spec.model = " ".into();
    assert!(matches!(
        blank.open().await,
        Err(OpenError::Build(BuildError::Invalid(_)))
    ));
}

#[tokio::test]
async fn a_postgres_failure_never_shows_the_password() {
    for dsn in [
        // An unreachable server waits out sqlx's 30 s acquire timeout, so
        // only configuration errors are checked here.
        "postgres://linch:hunter2-secret@127.0.0.1:notaport/db",
        "postgres://linch:hunter2-secret@127.0.0.1:5432/db?sslmode=bogus",
    ] {
        let mut config = scripted(json!([]), None);
        config.stores = StoreConfig::Postgres { dsn: dsn.into() };
        assert!(!format!("{config:?}").contains("hunter2"));
        let error = config.open().await.err().expect("no database there");
        assert!(matches!(error, OpenError::Store(_)), "{error:?}");
        let text = format!("{} {error:?}", chain(&error));
        assert!(!text.contains("hunter2"), "{text}");
    }
}

#[tokio::test]
async fn a_missing_key_names_its_variable() {
    if std::env::var_os("GEMINI_API_KEY").is_some() {
        eprintln!("skipped: GEMINI_API_KEY is set");
        return;
    }
    let config = config(json!({
        "namespace": "t",
        "spec": {"model": "m"},
        "provider": {"id": "gemini"},
    }));
    match config.open().await {
        Err(OpenError::MissingKey { provider, env }) => {
            assert_eq!((provider, env), ("gemini", "GEMINI_API_KEY"));
        }
        other => panic!("{:?}", other.err()),
    }
}
