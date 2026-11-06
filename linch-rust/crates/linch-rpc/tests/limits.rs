//! A tenant's caps, counted across all its connections: live runs, open
//! agents and open connections. Past a cap the request is refused with
//! `-32005` and nothing starts. A local connection has no caps.

mod common;

use std::future::pending;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use common::*;
use linch::PgPools;
use linch_rpc::protocol::{Cancelled, method};
use linch_rpc::{Access, Allowlists, ErrorObject, Tenant, Tokens, code};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream, Lines, ReadHalf};
use tokio::io::{WriteHalf, split};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

fn acme(allow: Allowlists) -> Arc<Tenant> {
    Arc::new(Tenant::new("acme", allow.scripted_provider(true)).unwrap())
}

/// Access as `tenant`, holding one of its connection places.
fn connected(tenant: &Arc<Tenant>) -> Access {
    Access::Tenant(Arc::clone(tenant).connect().unwrap())
}

/// The code and `data` of a refused call.
fn limit(error: linch_rpc::ClientError) -> (i64, Value) {
    match error {
        linch_rpc::ClientError::Rpc(error) => (error.code, error.data.unwrap_or_default()),
        other => panic!("expected an error object, got {other:?}"),
    }
}

/// A client whose host tool `wait` never answers, telling `arrived` when
/// a call comes in, and an agent on it whose runs each call `wait` once.
async fn waiting(access: Access, arrived: mpsc::UnboundedSender<()>) -> (Rpc, String) {
    let rpc = Rpc::remote_with(
        access,
        handler(move |_, _, _| {
            let _ = arrived.send(());
            pending::<Result<Value, ErrorObject>>()
        }),
    );
    let turns = (0..4)
        .map(|n| tool_turn(&format!("call_{n}"), "wait", json!({})))
        .collect();
    let mut config = hosted(
        turns,
        "default",
        json!({"tools": [host_tool("wait", "read")]}),
    );
    config["namespace"] = json!("acme");
    config.as_object_mut().unwrap().remove("permissions");
    let agent = rpc.open(config).await;
    (rpc, agent)
}

#[tokio::test]
async fn a_tenant_drives_at_most_max_runs_at_once_across_its_connections() {
    let acme = acme(Allowlists::new().max_runs(2));
    let (arrived, mut calls) = mpsc::unbounded_channel();
    let (mut first, agent_a) = waiting(connected(&acme), arrived.clone()).await;
    let (second, agent_b) = waiting(connected(&acme), arrived).await;
    let running = first.start(&agent_a, "s1", "wait").await;
    second.start(&agent_b, "s2", "wait").await;
    for _ in 0..2 {
        calls.recv().await.expect("each run waits in its tool");
    }
    let params = json!({"agent": agent_a, "session_id": "s3", "prompt": "wait"});
    let error = first.call(method::RUN_START, params.clone()).await;
    let (code, data) = limit(error.unwrap_err());
    assert_eq!(code, code::LIMIT_REACHED);
    assert_eq!(data, json!({"kind": "runs", "max": 2}));
    // A run that ends frees its place.
    let cancel = json!({"run_id": running});
    let cancelled: Cancelled = first
        .client
        .call(method::RUN_CANCEL, &cancel)
        .await
        .unwrap();
    assert!(cancelled.cancelled);
    first.until_end(&running).await;
    first.accepted(method::RUN_START, params).await;
    calls.recv().await.expect("the new run waits in its tool");
}

#[tokio::test]
async fn a_local_connection_has_no_run_cap() {
    let (arrived, mut calls) = mpsc::unbounded_channel();
    let (rpc, agent) = waiting(Access::Local, arrived).await;
    for session in ["s1", "s2", "s3"] {
        rpc.start(&agent, session, "wait").await;
        calls.recv().await.expect("the run waits in its tool");
    }
}

/// One connection's raw lines, and the task serving it.
struct Raw {
    writer: WriteHalf<DuplexStream>,
    lines: Lines<BufReader<ReadHalf<DuplexStream>>>,
    serving: JoinHandle<io::Result<()>>,
}

impl Raw {
    fn connect(access: Access) -> Self {
        let (ours, theirs) = tokio::io::duplex(64 * 1024);
        let (reader, writer) = split(theirs);
        let pools = Arc::new(PgPools::new());
        let stop = CancellationToken::new();
        let serving = tokio::spawn(linch_rpc::serve_lines(reader, writer, access, pools, stop));
        let (reader, writer) = split(ours);
        let lines = BufReader::new(reader).lines();
        Self {
            writer,
            lines,
            serving,
        }
    }

    /// Send a request and read the next line, `None` once the server closed.
    async fn call(&mut self, id: u64, method: &str, params: Value) -> Option<Value> {
        let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        let line = format!("{request}\n");
        // The server may have closed already; the read then says so.
        let _ = self.writer.write_all(line.as_bytes()).await;
        self.next().await
    }

    async fn next(&mut self) -> Option<Value> {
        let line = tokio::time::timeout(Duration::from_secs(10), self.lines.next_line());
        let line = line.await.expect("a line or the end").ok().flatten()?;
        Some(serde_json::from_str(&line).unwrap())
    }

    /// End the input and wait for the connection to stop.
    async fn close(mut self) {
        // The read half keeps the stream open, so dropping the writer
        // would not end the input.
        let _ = self.writer.shutdown().await;
        self.serving.await.unwrap().unwrap();
    }
}

fn cancel_params() -> Value {
    json!({"run_id": "r"})
}

#[tokio::test]
async fn a_tenant_holds_at_most_max_connections() {
    let acme = acme(Allowlists::new().max_connections(1));
    let mut first = Raw::connect(connected(&acme));
    let answer = first.call(1, method::RUN_CANCEL, cancel_params()).await;
    assert_eq!(answer.unwrap()["result"], json!({"cancelled": false}));
    // The place is refused before a connection is served.
    let refused = Arc::clone(&acme).connect().unwrap_err();
    assert_eq!(refused.code, code::LIMIT_REACHED);
    assert_eq!(refused.data, Some(json!({"kind": "connections", "max": 1})));
    first.close().await;
    let mut third = Raw::connect(connected(&acme));
    let answer = third.call(1, method::RUN_CANCEL, cancel_params()).await;
    assert_eq!(answer.unwrap()["result"], json!({"cancelled": false}));
    third.close().await;
}

#[tokio::test]
async fn a_login_past_the_connection_cap_is_refused_under_its_id() {
    let mut tokens = Tokens::new();
    let acme = acme(Allowlists::new().max_connections(1));
    tokens.insert("tok-acme-p11e-0001", acme).unwrap();
    let tokens = Arc::new(tokens);
    let auth = json!({"token": "tok-acme-p11e-0001"});
    let mut first = Raw::connect(Access::Login(Arc::clone(&tokens)));
    let answer = first.call(1, method::CONNECTION_AUTH, auth.clone()).await;
    assert_eq!(answer.unwrap()["result"], json!({"tenant": "acme"}));
    let mut second = Raw::connect(Access::Login(tokens));
    let refused = second.call(7, method::CONNECTION_AUTH, auth).await.unwrap();
    assert_eq!(refused["id"], 7);
    assert_eq!(refused["error"]["code"], code::LIMIT_REACHED);
    assert_eq!(refused["error"]["data"]["kind"], "connections");
    assert!(second.next().await.is_none(), "the connection closes");
    second.close().await;
    first.close().await;
}

/// A scripted agent of tenant `acme` with nothing to run.
fn idle() -> Value {
    let mut config = scripted(vec![], None, "default");
    config["namespace"] = json!("acme");
    config
}

#[tokio::test]
async fn a_tenant_keeps_at_most_max_agents_open_across_its_connections() {
    let acme = acme(Allowlists::new().max_agents(2));
    let first = Rpc::remote(connected(&acme));
    let mut second = Raw::connect(connected(&acme));
    let agent = first.open(idle()).await;
    let opened = second.call(1, method::AGENT_OPEN, idle()).await.unwrap();
    assert!(opened["result"]["agent"].is_string(), "{opened}");
    let error = first.call(method::AGENT_OPEN, idle()).await;
    let (code, data) = limit(error.unwrap_err());
    assert_eq!(code, code::LIMIT_REACHED);
    assert_eq!(data, json!({"kind": "agents", "max": 2}));
    // A closed agent gives its place back.
    let close = json!({"agent": agent});
    first.call(method::AGENT_CLOSE, close).await.unwrap();
    first.open(idle()).await;
    // So do the agents of a connection that ends.
    second.close().await;
    first.open(idle()).await;
}

#[tokio::test]
async fn a_local_connection_has_no_agent_cap() {
    let rpc = Rpc::remote(Access::Local);
    for _ in 0..=linch_rpc::DEFAULT_MAX_AGENTS {
        rpc.open(scripted(vec![], None, "default")).await;
    }
}
