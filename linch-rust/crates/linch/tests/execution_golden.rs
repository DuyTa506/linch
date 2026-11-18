//! The default `local` execution backend is byte-identical to the Bash and
//! file tools before execution backends existed: the same tool definitions,
//! the same run contract and the same config echo. The digests below were
//! taken on the base before the seam was added.

use linch::canonical::digest;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, ToolRegistry};
use serde_json::{Value, json};

fn config(workspace: &std::path::Path) -> AgentConfig {
    serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted",
                     "turns": [TurnBuilder::new("scripted-model").text("ok").build()]},
        "workspace": workspace,
    }))
    .unwrap()
}

/// Digest of the workspace tool definitions and scopes, in registry order.
const TOOLS_DIGEST: &str = "20279d647b8dbeafd787458240af305a8396ad6b8083c6f60ee3ae76d5a6d1c8";

/// The run contract of a workspace agent with the default backend.
const CONTRACT: &str = "8c3e125852e14cd24f89242447345ccff688eca402c2cde1c78a68602446401f";

#[tokio::test]
async fn the_local_backend_keeps_the_tools_and_the_contract() {
    let dir = tempfile::tempdir().unwrap();
    let tools = std::sync::Arc::new(ToolRegistry::new());
    let hooks = std::sync::Arc::new(linch::Hooks::default());
    let opened = config(dir.path())
        .open_with(tools.clone(), hooks, &linch::PgPools::new())
        .await
        .unwrap();
    let definitions: Vec<Value> = (tools.snapshot().definitions().iter())
        .map(|definition| serde_json::to_value(definition).unwrap())
        .collect();
    let tools = digest(&Value::Array(definitions)).unwrap().to_string();
    let contract = opened.agent().contract().to_string();
    assert_eq!(
        (tools.as_str(), contract.as_str()),
        (TOOLS_DIGEST, CONTRACT)
    );
}

#[test]
fn the_local_backend_is_not_echoed() {
    let dir = tempfile::tempdir().unwrap();
    let echo = serde_json::to_value(config(dir.path())).unwrap();
    let mut keys: Vec<&str> = echo
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        ["namespace", "provider", "spec", "stores", "workspace"]
    );
}
