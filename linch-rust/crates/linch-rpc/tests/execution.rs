//! A tenant's execution backends: a config may name only one the server
//! lists (by default only `local`), and a config naming none gets the
//! first, so a tenant can be held to a sandbox. A local connection keeps
//! its config's choice.

mod common;

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use common::*;
use linch::{ExecutionConfig, PermissionMode, SandboxPolicy};
use linch_rpc::protocol::method;
use linch_rpc::{Access, Allowlists, BackgroundAgents, Tenant};
use linch_worker::{Worker, WorkerConfig};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

/// Tenant `acme` that may run `Bash` without asking in `root`, with the
/// execution backends `backends`.
fn acme(root: &Path, backends: &[ExecutionConfig]) -> Access {
    tenant("acme", acme_allow(root, backends))
}

fn acme_allow(root: &Path, backends: &[ExecutionConfig]) -> Allowlists {
    Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[PermissionMode::SkipDangerous])
        .workspace_roots(&[root.to_path_buf()])
        .unwrap()
        .execution(backends)
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

/// A background run is admitted again by the worker's tenant: a stored
/// definition naming no backend runs in the sandbox the worker's server
/// holds the tenant to, whatever the server that queued it allowed.
#[tokio::test]
async fn a_background_run_gets_the_workers_execution_backend() {
    let Some(db) = common::pg::TestDb::empty("exec_background").await else {
        return;
    };
    let dir = workspace();
    let held = ExecutionConfig::Sandbox(SandboxPolicy::default());
    let probe = Rpc::remote(acme(dir.path(), std::slice::from_ref(&held)));
    let opened = probe
        .call(
            linch_rpc::protocol::method::AGENT_OPEN,
            config(dir.path(), "true", Value::Null),
        )
        .await;
    if let Err(error) = opened {
        eprintln!("skipped: {error}");
        db.drop().await;
        return;
    }
    let with_db = |allow: Allowlists| {
        allow
            .postgres_dsns(std::slice::from_ref(&db.dsn))
            .unwrap()
            .max_queued_jobs(Some(4))
    };
    let local = [ExecutionConfig::Local {}];
    let queuing = Tenant::new("acme", with_db(acme_allow(dir.path(), &local))).unwrap();
    let rpc = Rpc::remote(Access::Tenant(Arc::new(queuing).connect().unwrap()));
    let mut definition = config(dir.path(), "echo x > ../escaped", Value::Null);
    definition["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    let agent = rpc.open(definition).await;
    let queued = rpc
        .call(
            method::JOB_ENQUEUE,
            json!({"agent": agent, "session_id": "s1", "prompt": "go"}),
        )
        .await
        .unwrap();
    let job_id = queued["job_id"].as_i64().unwrap();

    let serving = Tenant::new("acme", with_db(acme_allow(dir.path(), &[held]))).unwrap();
    let timeout = linch::postgres::CONNECT_TIMEOUT;
    let pool = linch::postgres::connect(&db.dsn, timeout, 4.try_into().unwrap()).await;
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::new(serving));
    let mut worker = WorkerConfig::new("w1");
    worker.poll_interval = Duration::from_millis(50);
    let worker = Worker::new(pool.unwrap(), worker).definitions(factory.roots(), Arc::new(factory));
    let shutdown = CancellationToken::new();
    let running = tokio::spawn(worker.run(shutdown.clone()));
    let mut state = String::new();
    for _ in 0..400 {
        let got = rpc
            .call(method::JOB_GET, json!({"agent": agent, "job_id": job_id}))
            .await
            .unwrap();
        state = got["state"].as_str().unwrap_or_default().to_owned();
        if state == "done" || state == "failed" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    shutdown.cancel();
    running.await.unwrap().unwrap();
    db.drop().await;
    assert_eq!(state, "done");
    assert!(!dir.path().join("escaped").exists(), "Bash ran unconfined");
}
