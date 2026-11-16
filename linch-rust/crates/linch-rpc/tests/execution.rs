//! A tenant's execution backends: a config may name only one the server
//! lists (by default only `local`), and a config naming none gets the
//! first, so a tenant can be held to a sandbox. A local connection keeps
//! its config's choice.

mod common;

use std::path::Path;

use common::*;
use linch::{ExecutionConfig, PermissionMode, SandboxPolicy};
use linch_rpc::{Access, Allowlists};
use serde_json::{Value, json};

/// Tenant `acme` that may run `Bash` without asking in `root`, with the
/// execution backends `backends`.
fn acme(root: &Path, backends: &[ExecutionConfig]) -> Access {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[PermissionMode::SkipDangerous])
        .workspace_roots(&[root.to_path_buf()])
        .unwrap()
        .execution(backends);
    tenant("acme", allow)
}

/// A config in `root/ws` whose one turn runs `command` in `Bash`.
fn config(root: &Path, command: &str, execution: Value) -> Value {
    let turns = vec![
        tool_turn("c1", "Bash", json!({"command": command})),
        text_turn("done"),
    ];
    let mut config = scripted(turns, Some(&root.join("ws")), "skip_dangerous");
    config["namespace"] = json!("acme");
    if !execution.is_null() {
        config["execution"] = execution;
    }
    config
}

fn workspace() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("ws")).unwrap();
    std::fs::write(dir.path().join("secret"), "hidden").unwrap();
    dir
}

/// The Bash result of one run of `config`.
async fn bash(rpc: &mut Rpc, config: Value) -> String {
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    tool_results(&ended)[0].to_string()
}

#[tokio::test]
async fn a_tenant_may_name_only_its_listed_backends() {
    let dir = workspace();
    let rpc = Rpc::remote(acme(dir.path(), &[ExecutionConfig::Local {}]));
    let sandbox = json!({"backend": "sandbox"});
    let kind = refused(&rpc, config(dir.path(), "true", sandbox)).await;
    assert_eq!(kind.as_deref(), Some("execution"));
    let local = json!({"backend": "local"});
    assert_eq!(refused(&rpc, config(dir.path(), "true", local)).await, None);
    assert_eq!(
        refused(&rpc, config(dir.path(), "true", Value::Null)).await,
        None
    );
    // The operator's policy, not the client's: other limits are refused.
    let listed = ExecutionConfig::Sandbox(SandboxPolicy::default());
    let rpc = Rpc::remote(acme(dir.path(), &[listed]));
    let looser = json!({"backend": "sandbox", "network": true});
    let kind = refused(&rpc, config(dir.path(), "true", looser)).await;
    assert_eq!(kind.as_deref(), Some("execution"));
    let local = json!({"backend": "local"});
    let kind = refused(&rpc, config(dir.path(), "true", local)).await;
    assert_eq!(kind.as_deref(), Some("execution"));
    let rpc = Rpc::remote(acme(dir.path(), &[]));
    let kind = refused(&rpc, config(dir.path(), "true", Value::Null)).await;
    assert_eq!(kind.as_deref(), Some("execution"));
}

/// A tenant held to the sandbox runs `Bash` there even when its config
/// names no backend. Skips where the kernel cannot enforce the sandbox.
#[tokio::test]
async fn a_tenant_held_to_the_sandbox_runs_bash_in_it() {
    let dir = workspace();
    let held = ExecutionConfig::Sandbox(SandboxPolicy::default());
    let mut rpc = Rpc::remote(acme(dir.path(), &[held]));
    let config = config(dir.path(), "cat ../secret", Value::Null);
    match rpc
        .call(linch_rpc::protocol::method::AGENT_OPEN, config.clone())
        .await
    {
        Ok(_) => {}
        Err(error) => {
            eprintln!("skipped: {error}");
            return;
        }
    }
    let result = bash(&mut rpc, config).await;
    assert!(result.contains("Permission denied"), "{result}");
    let mut local = Rpc::remote(acme(dir.path(), &[ExecutionConfig::Local {}]));
    let config = self::config(dir.path(), "cat ../secret", Value::Null);
    let result = bash(&mut local, config).await;
    assert!(result.contains("hidden"), "{result}");
}
