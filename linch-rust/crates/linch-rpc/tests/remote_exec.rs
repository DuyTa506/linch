//! The `remote` execution backend over the connection: `Bash` commands run
//! in the client that opened the agent, through `exec.start`, `exec.read`
//! and `exec.kill`. The client here is scripted; the Python client has a
//! host that runs real commands.

mod common;

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::ExecutionConfig;
use linch_rpc::protocol::{ExecKillParams, ExecReadParams, ExecStartParams, method};
use linch_rpc::{Allowlists, BackgroundAgents, ErrorObject, code};
use linch_worker::{AgentFactory, FactoryError};
use serde_json::{Value, json};
use tokio::sync::mpsc;

/// What the scripted host does with a command.
#[derive(Clone, Copy)]
enum Script {
    /// Prints `hi` and `oops`, exits 3 on the first read.
    Quick,
    /// Runs until killed: reads wait their time and answer empty; a kill is
    /// answered with the signal.
    Hang,
    /// Starts, then answers every read and kill with an error.
    Lost,
}

type Calls = mpsc::UnboundedReceiver<(String, Value)>;

/// A client playing the host of a `remote` backend, and the requests it got.
fn host(script: Script) -> (Rpc, Calls) {
    let (sender, calls) = mpsc::unbounded_channel();
    let rpc = Rpc::with_handler(serving(script, sender));
    (rpc, calls)
}

fn serving(
    script: Script,
    sender: mpsc::UnboundedSender<(String, Value)>,
) -> Arc<dyn linch_rpc::RequestHandler> {
    handler(move |method, params, cancel| {
        let sender = sender.clone();
        async move {
            let _ = sender.send((method.clone(), Value::Object(params.clone())));
            let lost = || Err(ErrorObject::new(-32000, "the host lost the command"));
            match (method.as_str(), script) {
                (method::EXEC_START, _) => Ok(json!({})),
                (method::EXEC_READ, Script::Quick) => {
                    Ok(json!({"stdout": "hi\n", "stderr": "oops\n", "exit": {"code": 3}}))
                }
                (method::EXEC_READ, Script::Hang) => {
                    let wait = params["wait_ms"].as_u64().unwrap();
                    tokio::select! {
                        () = tokio::time::sleep(Duration::from_millis(wait)) => Ok(json!({})),
                        () = cancel.cancelled() => Ok(json!({})),
                    }
                }
                (method::EXEC_KILL, Script::Hang) => Ok(json!({"exit": {"signal": 9}})),
                (method::EXEC_READ | method::EXEC_KILL, Script::Lost) => lost(),
                (other, _) => panic!("unexpected {other}"),
            }
        }
    })
}

fn bash_config(root: &Path, input: Value, execution: Value) -> Value {
    let turns = vec![tool_turn("c1", "Bash", input), text_turn("done")];
    let mut config = scripted(turns, Some(root), "skip_dangerous");
    config["execution"] = execution;
    config
}

fn remote() -> Value {
    json!({"backend": "remote"})
}

/// The next request of `method`, within 10 s.
async fn next(calls: &mut Calls, wanted: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let (method, params) = calls.recv().await.expect("the host was closed");
            if method == wanted {
                return params;
            }
        }
    })
    .await
    .unwrap_or_else(|_| panic!("no {wanted} arrived"))
}

#[tokio::test]
async fn a_command_runs_in_the_client_and_its_output_comes_back() {
    let dir = tempfile::tempdir().unwrap();
    let (mut rpc, mut calls) = host(Script::Quick);
    let input = json!({"command": "echo hi", "timeout_ms": 5000});
    let agent = rpc.open(bash_config(dir.path(), input, remote())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;

    let results = tool_results(&ended);
    let text = results[0].to_string();
    assert!(
        text.contains("hi\\n[stderr]\\noops\\n[exit code 3]"),
        "{text}"
    );
    assert_eq!(results[0]["is_error"], json!(true), "exit 3 is an error");

    let start: ExecStartParams = serde_json::from_value(next(&mut calls, "exec.start").await)
        .expect("exec.start params are the documented ones");
    assert_eq!(start.agent, agent);
    assert_eq!(start.command, "echo hi");
    assert_eq!(start.session_id.as_str(), "s1");
    assert_eq!(start.cwd, None);
    assert!(!start.background);
    for server_only in ["PATH", "HOME", "USER", "SHELL", "TMPDIR"] {
        assert!(!start.env.contains_key(server_only), "{:?}", start.env);
    }
    let read: ExecReadParams = serde_json::from_value(next(&mut calls, "exec.read").await).unwrap();
    assert_eq!(
        (read.agent.as_str(), read.exec_id.as_str()),
        (agent.as_str(), start.exec_id.as_str())
    );
    assert!(read.wait_ms > 0);
}

/// Two principals on one agent share the client's exec host: each command
/// reaches it with the principal of the run that made it, so a host that
/// serves several principals can scope or deny per principal. A run with no
/// principal sends none.
#[tokio::test]
async fn each_command_reaches_the_host_with_its_runs_principal() {
    let dir = tempfile::tempdir().unwrap();
    let (mut rpc, mut calls) = host(Script::Quick);
    let input = json!({"command": "echo hi", "timeout_ms": 5000});
    let mut config = bash_config(dir.path(), input.clone(), remote());
    let call = |id: &str| tool_turn(id, "Bash", input.clone());
    let turns = [
        call("c1"),
        text_turn("a"),
        call("c2"),
        text_turn("b"),
        call("c3"),
        text_turn("c"),
    ];
    config["provider"]["turns"] = json!(turns);
    let agent = rpc.open(config).await;
    let ada = json!({"tenant": "acme", "user": "ada", "roles": ["support"]});
    let bob = json!({"tenant": "acme", "user": "bob"});
    for (session, principal) in [("s1", Some(&ada)), ("s2", Some(&bob)), ("s3", None)] {
        let mut params = json!({"agent": agent, "session_id": session, "prompt": "go"});
        if let Some(principal) = principal {
            params["principal"] = principal.clone();
        }
        let run_id = rpc.accepted(method::RUN_START, params).await;
        rpc.until_end(&run_id).await;
    }
    let mut seen = Vec::new();
    for _ in 0..3 {
        seen.push(next(&mut calls, "exec.start").await);
    }
    assert_eq!(seen[0]["principal"], ada);
    assert_eq!(
        seen[1]["principal"],
        json!({"tenant": "acme", "user": "bob", "roles": []})
    );
    assert_eq!(seen[2].get("principal"), None, "a run with none sends none");
    let start: ExecStartParams = serde_json::from_value(seen[0].clone()).unwrap();
    assert_eq!(start.principal.unwrap().user(), "ada");
}

#[tokio::test]
async fn a_run_cancel_tells_the_host_to_kill() {
    let dir = tempfile::tempdir().unwrap();
    let (mut rpc, mut calls) = host(Script::Hang);
    let input = json!({"command": "sleep 30"});
    let agent = rpc.open(bash_config(dir.path(), input, remote())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let start: ExecStartParams =
        serde_json::from_value(next(&mut calls, "exec.start").await).unwrap();
    next(&mut calls, "exec.read").await;
    rpc.call(method::RUN_CANCEL, json!({"run_id": run_id}))
        .await
        .unwrap();
    let kill: ExecKillParams = serde_json::from_value(next(&mut calls, "exec.kill").await).unwrap();
    assert_eq!(kill.exec_id, start.exec_id);
    rpc.until_end(&run_id).await;
}

#[tokio::test]
async fn a_timeout_tells_the_host_to_kill_and_the_model_reads_the_timeout() {
    let dir = tempfile::tempdir().unwrap();
    let (mut rpc, mut calls) = host(Script::Hang);
    let input = json!({"command": "sleep 30", "timeout_ms": 300});
    let agent = rpc.open(bash_config(dir.path(), input, remote())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    next(&mut calls, "exec.kill").await;
    let text = tool_results(&ended)[0].to_string();
    assert!(
        text.contains("timed out after 300 ms; process group killed"),
        "{text}"
    );
}

#[tokio::test]
async fn a_host_that_fails_mid_command_is_interrupted_never_a_result() {
    let dir = tempfile::tempdir().unwrap();
    let (mut rpc, _calls) = host(Script::Lost);
    let input = json!({"command": "sleep 30", "timeout_ms": 5000});
    let agent = rpc.open(bash_config(dir.path(), input, remote())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let text = tool_results(&ended)[0].to_string();
    assert!(text.contains("[interrupted"), "{text}");
    for made_up in ["exit code", "killed by signal", "timed out"] {
        assert!(!text.contains(made_up), "{text}");
    }
}

#[tokio::test]
async fn a_client_that_serves_no_exec_methods_fails_the_command_to_start() {
    let dir = tempfile::tempdir().unwrap();
    let mut rpc = Rpc::new();
    let input = json!({"command": "echo hi"});
    let agent = rpc.open(bash_config(dir.path(), input, remote())).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    let text = tool_results(&ended)[0].to_string();
    assert!(
        text.contains("could not start the remote command"),
        "{text}"
    );
}

#[tokio::test]
async fn the_file_tools_are_not_there_unless_the_workspace_is_shared() {
    let dir = tempfile::tempdir().unwrap();
    let read = json!({"file_path": dir.path().join("a").display().to_string()});
    for (execution, has_read) in [
        (remote(), false),
        (json!({"backend": "remote", "shared_workspace": true}), true),
    ] {
        let turns = vec![tool_turn("c1", "Read", read.clone()), text_turn("done")];
        let mut config = scripted(turns, Some(dir.path()), "skip_dangerous");
        config["execution"] = execution;
        let (mut rpc, _calls) = host(Script::Quick);
        let agent = rpc.open(config).await;
        let run_id = rpc.start(&agent, "s1", "go").await;
        let ended = rpc.until_end(&run_id).await;
        let text = tool_results(&ended)[0].to_string();
        assert_eq!(!text.contains("is not registered"), has_read, "{text}");
    }
}

fn acme(root: &Path, backends: &[ExecutionConfig]) -> linch_rpc::Access {
    let allow = Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[linch::PermissionMode::SkipDangerous])
        .workspace_roots(&[root.to_path_buf()])
        .unwrap()
        .execution(backends);
    tenant("acme", allow)
}

#[tokio::test]
async fn a_tenant_may_use_the_remote_backend_only_when_its_list_names_it() {
    let dir = tempfile::tempdir().unwrap();
    let ws = dir.path();
    let config = |execution: Value| {
        let mut config = bash_config(ws, json!({"command": "true"}), execution);
        config["namespace"] = json!("acme");
        config
    };
    // Denied by default, whatever the policy asked for.
    let rpc = Rpc::remote(acme(ws, &[ExecutionConfig::Local {}]));
    assert_eq!(
        refused(&rpc, config(remote())).await.as_deref(),
        Some("execution")
    );
    let policy = linch::RemotePolicy {
        cwd: Some("/srv".into()),
        shared_workspace: false,
    };
    let listed = ExecutionConfig::Remote(policy);
    let rpc = Rpc::remote(acme(ws, std::slice::from_ref(&listed)));
    // The operator's policy exactly: another cwd, or none, is refused.
    let other = json!({"backend": "remote", "cwd": "/elsewhere"});
    assert_eq!(
        refused(&rpc, config(other)).await.as_deref(),
        Some("execution")
    );
    assert_eq!(
        refused(&rpc, config(remote())).await.as_deref(),
        Some("execution")
    );
    let local = json!({"backend": "local"});
    assert_eq!(
        refused(&rpc, config(local)).await.as_deref(),
        Some("execution")
    );
    let exact = json!({"backend": "remote", "cwd": "/srv"});
    assert_eq!(refused(&rpc, config(exact)).await, None);
    // Naming none gets the first entry, so a tenant is held to its host.
    assert_eq!(refused(&rpc, config(Value::Null)).await, None);
}

#[tokio::test]
async fn a_tenant_held_to_a_remote_host_runs_bash_there() {
    let dir = tempfile::tempdir().unwrap();
    let listed = ExecutionConfig::Remote(linch::RemotePolicy::default());
    let (sender, mut calls) = mpsc::unbounded_channel();
    let mut rpc = Rpc::remote_with(acme(dir.path(), &[listed]), serving(Script::Quick, sender));
    let mut config = bash_config(dir.path(), json!({"command": "echo hi"}), Value::Null);
    config["namespace"] = json!("acme");
    config.as_object_mut().unwrap().remove("execution");
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "go").await;
    let ended = rpc.until_end(&run_id).await;
    next(&mut calls, "exec.start").await;
    assert!(
        tool_results(&ended)[0]
            .to_string()
            .contains("[exit code 3]")
    );
}

fn on_postgres(db: &TestDb, dir: &Path, execution: Value) -> Value {
    let mut config = bash_config(dir, json!({"command": "true"}), execution);
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

#[tokio::test]
async fn an_agent_with_a_remote_backend_cannot_be_scheduled_or_queued() {
    let Some(db) = TestDb::empty("remote_not_background").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let (rpc, _calls) = host(Script::Quick);
    let agent = rpc.open(on_postgres(&db, dir.path(), remote())).await;
    let refusals = [
        (
            method::SCHEDULE_CREATE,
            json!({"agent": agent, "prompt": "p", "interval_ms": 60_000}),
        ),
        (
            method::JOB_ENQUEUE,
            json!({"agent": agent, "session_id": "s", "prompt": "p"}),
        ),
    ];
    for (method, params) in refusals {
        let Err(linch_rpc::ClientError::Rpc(error)) = rpc.call(method, params).await else {
            panic!("{method} was accepted");
        };
        assert_eq!(error.code, code::NOT_BACKGROUND, "{method}");
        let data = error.data.unwrap();
        assert_eq!(data["reason"], json!("remote_execution"), "{method}");
        assert!(
            error.message.contains("remote execution"),
            "{}",
            error.message
        );
    }
    db.drop().await;
}

/// Only an agent that has `Bash` needs the host: without `Bash` the backend
/// is never used, so it does not stop the agent from running in the
/// background.
#[tokio::test]
async fn an_agent_without_bash_can_be_scheduled_whatever_backend_it_names() {
    let Some(db) = TestDb::empty("remote_no_bash").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let (rpc, _calls) = host(Script::Quick);
    for name in ["bash off", "no workspace"] {
        let mut config = on_postgres(&db, dir.path(), remote());
        match name {
            "bash off" => config["bash"] = json!(false),
            _ => {
                config.as_object_mut().unwrap().remove("workspace");
            }
        }
        let agent = rpc.open(config).await;
        let created = rpc
            .call(
                method::SCHEDULE_CREATE,
                json!({"agent": agent, "prompt": "p", "interval_ms": 60_000}),
            )
            .await;
        assert!(created.is_ok(), "{name}: {created:?}");
        let queued = rpc
            .call(
                method::JOB_ENQUEUE,
                json!({"agent": agent, "session_id": "s", "prompt": "p"}),
            )
            .await;
        assert!(queued.is_ok(), "{name}: {queued:?}");
    }
    db.drop().await;
}

/// A tenant whose first `execution` entry is remote gets that backend filled
/// in; an agent of it without `Bash` still runs in a worker.
#[tokio::test]
async fn a_worker_opens_a_bash_less_agent_of_a_tenant_held_to_a_remote_host() {
    let Some(db) = TestDb::empty("remote_worker_no_bash").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let listed = ExecutionConfig::Remote(linch::RemotePolicy::default());
    let allow = Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[linch::PermissionMode::SkipDangerous])
        .workspace_roots(&[dir.path().to_path_buf()])
        .unwrap()
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
        .execution(&[listed]);
    let serving = linch_rpc::Tenant::new("acme", allow).unwrap();
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::new(serving));
    for named in [true, false] {
        let mut definition =
            on_postgres(&db, dir.path(), if named { remote() } else { Value::Null });
        definition["namespace"] = json!("acme");
        definition["stores"]["dsn"] = json!("");
        definition["bash"] = json!(false);
        if !named {
            definition.as_object_mut().unwrap().remove("execution");
        }
        let opened = factory.open("acme", &definition.to_string()).await;
        assert!(opened.is_ok(), "named: {named}");
    }
    // With `Bash` it is still refused.
    let mut definition = on_postgres(&db, dir.path(), Value::Null);
    definition["namespace"] = json!("acme");
    definition["stores"]["dsn"] = json!("");
    definition.as_object_mut().unwrap().remove("execution");
    let opened = factory.open("acme", &definition.to_string()).await;
    assert!(matches!(opened, Err(FactoryError::Refused(_))));
    db.drop().await;
}

#[tokio::test]
async fn a_worker_refuses_a_definition_with_a_remote_backend() {
    let Some(db) = TestDb::empty("remote_worker").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let listed = ExecutionConfig::Remote(linch::RemotePolicy::default());
    let allow = Allowlists::new()
        .scripted_provider(true)
        .exec_tools(true)
        .permission_modes(&[linch::PermissionMode::SkipDangerous])
        .workspace_roots(&[dir.path().to_path_buf()])
        .unwrap()
        .postgres_dsns(std::slice::from_ref(&db.dsn))
        .unwrap()
        .execution(&[listed]);
    let serving = linch_rpc::Tenant::new("acme", allow).unwrap();
    let factory = BackgroundAgents::new(db.dsn.clone()).tenant(Arc::new(serving));
    let mut definition = on_postgres(&db, dir.path(), remote());
    definition["namespace"] = json!("acme");
    definition["stores"]["dsn"] = json!("");
    let opened = factory.open("acme", &definition.to_string()).await;
    let Err(FactoryError::Refused(reason)) = opened else {
        panic!("a worker opened an agent that needs a live host");
    };
    assert!(reason.contains("worker"), "{reason}");
    db.drop().await;
}
