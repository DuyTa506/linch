//! The `execution` config: which backend a workspace agent's `Bash` runs
//! through, part of the run contract when it is not `local`.

use std::path::Path;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, OpenError, RunOptions, SessionId};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn config(root: &Path, command: &str, execution: Value) -> AgentConfig {
    let call = json!({"command": command});
    let turns = [
        TurnBuilder::new(MODEL)
            .tool_call("c1", "Bash", &call)
            .build(),
        TurnBuilder::new(MODEL).text("done").build(),
    ];
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "workspace": root,
        "permissions": {"mode": "skip_dangerous", "project_root": root},
    });
    if !execution.is_null() {
        value["execution"] = execution;
    }
    serde_json::from_value(value).unwrap()
}

/// The text of the one Bash result of a run of session `s1`.
async fn bash_result(config: AgentConfig) -> String {
    let opened = config.open().await.unwrap();
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let mut result = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            result = Some(serde_json::to_string(&end.result).unwrap());
        }
    }
    opened.agent().close_session(&SessionId::new("s1"));
    result.expect("Bash ran")
}

/// A sandboxed agent's `Bash` cannot read beside its workspace; a local
/// one can. Skips where the kernel cannot enforce the sandbox.
#[tokio::test]
async fn a_sandboxed_agent_runs_bash_in_the_sandbox() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("root");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(dir.path().join("secret"), "hidden").unwrap();
    let command = "cat ../secret";
    let sandboxed = config(&root, command, json!({"backend": "sandbox"}));
    let opened = sandboxed.clone().open().await;
    if let Err(OpenError::Execution(error)) = &opened {
        eprintln!("skipped: {error}");
        return;
    }
    drop(opened);
    let confined = bash_result(sandboxed).await;
    assert!(confined.contains("Permission denied"), "{confined}");
    let local = bash_result(config(&root, command, Value::Null)).await;
    assert!(local.contains("hidden"), "{local}");
}

#[tokio::test]
async fn a_non_local_backend_changes_the_contract_and_local_does_not() {
    let dir = tempfile::tempdir().unwrap();
    let contract = |execution: Value| {
        let config = config(dir.path(), "true", execution);
        async move { config.open().await.map(|opened| opened.agent().contract()) }
    };
    let default = contract(Value::Null).await.unwrap();
    let local = contract(json!({"backend": "local"})).await.unwrap();
    assert_eq!(default, local);
    match contract(json!({"backend": "sandbox"})).await {
        Ok(sandbox) => {
            assert_ne!(sandbox, default);
            let other = contract(json!({"backend": "sandbox", "memory_bytes": 1 << 30})).await;
            assert_ne!(other.unwrap(), sandbox);
        }
        Err(error) => eprintln!("skipped the sandbox: {error}"),
    }
}

#[tokio::test]
async fn an_unusable_backend_fails_the_open() {
    let dir = tempfile::tempdir().unwrap();
    let missing = json!({"backend": "docker", "docker_path": "/nonexistent/docker",
                         "image": " "});
    let error = config(dir.path(), "true", missing)
        .open()
        .await
        .unwrap_err();
    assert!(matches!(error, OpenError::Execution(_)), "{error}");
    // Without Bash the backend is never opened.
    let mut off = config(
        dir.path(),
        "true",
        json!({"backend": "docker", "image": " "}),
    );
    off.bash = false;
    assert!(off.open().await.is_ok());
}

#[test]
fn the_backend_is_echoed_unless_it_is_absent() {
    let dir = tempfile::tempdir().unwrap();
    let echo = serde_json::to_value(config(dir.path(), "true", json!({"backend": "sandbox"})));
    let execution = echo.unwrap()["execution"].clone();
    assert_eq!(execution["backend"], "sandbox");
    assert_eq!(execution["memory_bytes"], 2u64 << 30);
    let absent = serde_json::to_value(config(dir.path(), "true", Value::Null)).unwrap();
    assert!(absent.get("execution").is_none());
}
