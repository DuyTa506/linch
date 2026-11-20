//! Path rules judge a relative path against the directory the workspace
//! tools resolve it in (the workspace), not against `permissions.project_root`.

use std::path::Path;

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, RunOptions};
use serde_json::{Value, json};

/// One call of `tool` with `input` by an agent whose permissions name
/// `project_root` and deny `secrets/**` and `notes/**`, over `workspace`
/// (with the virtual filesystem on). Returns whether the call's result was
/// an error.
async fn is_refused(workspace: &Path, project_root: &Path, tool: &str, input: Value) -> bool {
    let turns = json!([
        TurnBuilder::new("scripted-model")
            .tool_call("call_1", tool, &input)
            .build(),
        TurnBuilder::new("scripted-model").text("done").build(),
    ]);
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": turns},
        "workspace": workspace,
        "filesystem": {},
        "permissions": {
            "mode": "skip_dangerous",
            "project_root": project_root,
            "layers": [{"name": "host", "rules": [
                {"type": "path", "paths": ["secrets/**", "notes/**"], "decision": "deny"},
            ]}],
        },
    }))
    .unwrap();
    let opened = config.open().await.unwrap();
    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let mut refused = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            refused = Some(end.result.is_error);
        }
    }
    refused.expect("the call ended")
}

async fn write_is_refused(workspace: &Path, project_root: &Path, file_path: &str) -> bool {
    let input = json!({"file_path": file_path, "content": "x"});
    is_refused(workspace, project_root, "Write", input).await
}

#[tokio::test]
async fn a_relative_path_is_judged_where_the_file_tools_resolve_it() {
    let dir = tempfile::tempdir().unwrap();
    let acme = dir.path().canonicalize().unwrap().join("acme");
    std::fs::create_dir_all(acme.join("app/secrets")).unwrap();
    let app = acme.join("app");

    // The workspace is the parent of the project root: `app/secrets/key`
    // lands in `<project_root>/secrets`, which the rule covers.
    assert!(write_is_refused(&acme, &app, "app/secrets/key").await);
    assert!(!acme.join("app/secrets/key").exists());
    assert!(!write_is_refused(&acme, &app, "app/notes-ok").await);
    assert!(acme.join("app/notes-ok").exists());

    // The workspace is below the project root: `secrets/key` lands in
    // `<project_root>/app/secrets`, which `secrets/**` does not name.
    assert!(!write_is_refused(&app, &acme, "secrets/key").await);
    assert!(app.join("secrets/key").exists());
}

#[tokio::test]
async fn a_project_root_that_is_the_workspace_judges_as_it_always_did() {
    let dir = tempfile::tempdir().unwrap();
    let acme = dir.path().canonicalize().unwrap().join("acme");
    std::fs::create_dir_all(&acme).unwrap();
    assert!(write_is_refused(&acme, &acme, "secrets/key").await);
    assert!(!write_is_refused(&acme, &acme, "notes-ok").await);
    assert!(!acme.join("secrets/key").exists());

    // The same directory spelled through a symlink: the project root and
    // the workspace keep naming one place.
    let link = dir.path().join("link");
    std::os::unix::fs::symlink(&acme, &link).unwrap();
    assert!(write_is_refused(&link, &link, "secrets/key").await);
    assert!(!acme.join("secrets/key").exists());
}

#[tokio::test]
async fn a_virtual_path_is_still_judged_against_the_project_root() {
    let dir = tempfile::tempdir().unwrap();
    let acme = dir.path().canonicalize().unwrap().join("acme");
    std::fs::create_dir_all(acme.join("app")).unwrap();
    // `write_file` keeps its files in the virtual filesystem, not the
    // workspace, so its `notes/a` is `<project_root>/notes/a` as before.
    let input = json!({"path": "notes/a", "content": "x"});
    assert!(is_refused(&acme, &acme.join("app"), "write_file", input).await);
}
