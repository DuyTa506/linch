//! The `remote` execution backend at the facade: it opens only with a host
//! to run its commands, enters the run contract through `Bash`'s
//! description, and registers file tools only when the policy shares the
//! workspace. An agent that does not use it keeps its contract.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use linch::provider::scripted::TurnBuilder;
use linch::{
    AgentConfig, ExecutionConfig, Hooks, OpenError, PgPools, RemoteChunk, RemoteCommand,
    RemoteError, RemoteExec, ToolRegistry,
};
use serde_json::{Value, json};

#[derive(Debug)]
struct Host;

#[async_trait]
impl RemoteExec for Host {
    async fn start(&self, _: RemoteCommand) -> Result<(), RemoteError> {
        Ok(())
    }
    async fn read(&self, _: &str, _: Duration) -> Result<RemoteChunk, RemoteError> {
        Ok(RemoteChunk::default())
    }
    async fn kill(&self, _: &str) -> Result<RemoteChunk, RemoteError> {
        Ok(RemoteChunk::default())
    }
}

fn config(workspace: &std::path::Path, execution: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted",
                     "turns": [TurnBuilder::new("scripted-model").text("ok").build()]},
        "workspace": workspace,
    });
    if !execution.is_null() {
        value["execution"] = execution;
    }
    serde_json::from_value(value).unwrap()
}

/// The tool names of the agent and its run contract.
async fn open(config: AgentConfig, host: Option<Arc<dyn RemoteExec>>) -> (Vec<String>, String) {
    let tools = Arc::new(ToolRegistry::new());
    let opened = config
        .open_with_remote(
            tools.clone(),
            Arc::new(Hooks::default()),
            &PgPools::new(),
            host,
        )
        .await
        .unwrap();
    let snapshot = tools.snapshot();
    let names = (snapshot.definitions().iter())
        .map(|definition| definition.name.clone())
        .collect();
    (names, opened.agent().contract().to_string())
}

fn host() -> Option<Arc<dyn RemoteExec>> {
    Some(Arc::new(Host))
}

#[tokio::test]
async fn without_a_host_a_remote_agent_does_not_open() {
    let dir = tempfile::tempdir().unwrap();
    let config = config(dir.path(), json!({"backend": "remote"}));
    for opened in [
        config.clone().open().await,
        config
            .open_with_remote(
                Arc::new(ToolRegistry::new()),
                Arc::new(Hooks::default()),
                &PgPools::new(),
                None,
            )
            .await,
    ] {
        let Err(OpenError::Execution(error)) = opened else {
            panic!("a remote agent opened with no host");
        };
        assert!(error.to_string().contains("worker"), "{error}");
    }
}

#[tokio::test]
async fn a_remote_agent_has_only_the_bash_tools_unless_the_workspace_is_shared() {
    let dir = tempfile::tempdir().unwrap();
    let (names, _) = open(config(dir.path(), json!({"backend": "remote"})), host()).await;
    assert_eq!(names, ["Bash", "BashOutput", "BashKill"]);
    let shared = json!({"backend": "remote", "shared_workspace": true});
    let (names, _) = open(config(dir.path(), shared), host()).await;
    assert_eq!(names.len(), 8, "{names:?}");
    let off = json!({"backend": "remote"});
    let mut no_bash = config(dir.path(), off);
    no_bash.bash = false;
    // `execution` is unused without `bash`: the file tools are the local ones.
    let (names, _) = open(no_bash, host()).await;
    assert_eq!(names, ["Read", "Write", "Edit", "Grep", "Glob"]);
}

/// The contract of a remote agent with each policy, taken when the backend
/// was added: a change of the description changes it.
const REMOTE_CONTRACT: &str = "929a84c9e09f2d14052cb8bc0412db0febedf65f02df4ff4d2dcf1dc103909a7";

#[tokio::test]
async fn the_remote_policy_is_part_of_the_run_contract() {
    let dir = tempfile::tempdir().unwrap();
    let mut contracts = Vec::new();
    for execution in [
        json!(null),
        json!({"backend": "remote"}),
        json!({"backend": "remote", "cwd": "/srv/work"}),
        json!({"backend": "remote", "shared_workspace": true}),
    ] {
        contracts.push(open(config(dir.path(), execution), host()).await.1);
    }
    let distinct: std::collections::BTreeSet<&String> = contracts.iter().collect();
    assert_eq!(distinct.len(), contracts.len(), "{contracts:?}");
    assert_eq!(contracts[1], REMOTE_CONTRACT);
}

#[test]
fn an_unset_backend_stays_off_the_wire_and_a_remote_one_is_echoed() {
    let dir = tempfile::tempdir().unwrap();
    let unset = serde_json::to_value(config(dir.path(), json!(null))).unwrap();
    assert!(unset.get("execution").is_none(), "{unset}");
    let remote = serde_json::to_value(config(dir.path(), json!({"backend": "remote"}))).unwrap();
    assert_eq!(remote["execution"], json!({"backend": "remote"}));
    let parsed: ExecutionConfig = serde_json::from_value(remote["execution"].clone()).unwrap();
    assert_eq!(parsed.kind(), "remote");
}
