//! The server's own lifecycle: the cap on calls in flight, a panicking run,
//! and the order of the teardown.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use linch::hooks::{ContextBuild, ContextBuildRequest, ContextBuildResponse};
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, CallbackError, Hook, Hooks, ToolRegistry};
use serde_json::json;
use tokio::sync::mpsc;

use super::*;
use crate::server::outgoing::Outgoing;

/// A server whose output lines land in the returned receiver.
fn server() -> (Server, mpsc::Receiver<Outgoing>) {
    let (sender, lines) = mpsc::channel(64);
    (
        Server::new(
            Outbox(sender),
            None,
            Arc::new(linch::PgPools::new()),
            &CancellationToken::new(),
        ),
        lines,
    )
}

async fn next(lines: &mut mpsc::Receiver<Outgoing>) -> JsonValue {
    let line = tokio::time::timeout(Duration::from_secs(5), lines.recv());
    let line = line.await.expect("a line").expect("open").into_line();
    serde_json::from_str(&line).unwrap()
}

fn request(id: u64, method: &str, params: JsonValue) -> String {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string()
}

#[tokio::test]
async fn past_the_cap_the_read_loop_waits_for_a_call_to_finish() {
    let (server, mut lines) = server();
    let held = Arc::clone(&server.0.permits)
        .acquire_many_owned(MAX_REQUESTS as u32)
        .await
        .unwrap();
    let cancel = request(1, method::RUN_CANCEL, json!({"run_id": "r"}));
    let receiving = server.receive(&cancel);
    tokio::pin!(receiving);
    let early = tokio::time::timeout(Duration::from_millis(100), &mut receiving).await;
    assert!(early.is_err(), "a call past the cap was read");
    drop(held);
    receiving.await;
    assert_eq!(
        next(&mut lines).await["result"],
        json!({"cancelled": false})
    );
}

/// A `context_build` hook that panics. A panicking tool no longer panics
/// the run (it becomes a tool error); a hook still does.
struct Panics;

#[async_trait]
impl Hook<ContextBuild> for Panics {
    fn policy_id(&self) -> &str {
        "panics"
    }

    fn policy_version(&self) -> u32 {
        1
    }

    async fn call(&self, _: &ContextBuildRequest) -> Result<ContextBuildResponse, CallbackError> {
        panic!("a hook bug");
    }
}

#[tokio::test]
async fn a_run_that_panics_ends_with_an_error_and_frees_its_id() {
    let (server, mut lines) = server();
    let turn = TurnBuilder::new("m").text("done").build();
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t", "spec": {"model": "m"},
        "provider": {"id": "scripted", "turns": [turn]}}))
    .unwrap();
    let hooks = Hooks::default();
    let _ = hooks.context_build.register(Arc::new(Panics)).unwrap();
    let opened = config
        .open_with(
            Arc::new(ToolRegistry::new()),
            Arc::new(hooks),
            &linch::PgPools::new(),
        )
        .await
        .unwrap();
    lock(&server.0.tables)
        .agents
        .insert("agent-1".into(), opened);

    let start = json!({"agent": "agent-1", "session_id": "s1", "prompt": "go"});
    server.receive(&request(1, method::RUN_START, start)).await;
    let run_id = next(&mut lines).await["result"]["run_id"].clone();
    let end = loop {
        let message = next(&mut lines).await;
        if message["method"] == method::RUN_END {
            break message;
        }
    };
    assert_eq!(
        end["params"]["error"]["code"],
        code::INTERNAL_ERROR,
        "{end}"
    );
    assert!(lock(&server.0.tables).runs.is_empty());
    let resume = json!({"agent": "agent-1", "run_id": run_id});
    server
        .receive(&request(2, method::RUN_RESUME, resume))
        .await;
    let resumed = next(&mut lines).await;
    assert_eq!(resumed["result"]["run_id"], run_id, "not busy: {resumed}");
}

/// Records at drop whether the peer was already closed.
struct Witness {
    peer: Peer,
    closed_first: Arc<AtomicBool>,
}

impl Drop for Witness {
    fn drop(&mut self) {
        self.closed_first
            .store(self.peer.is_closed(), Ordering::SeqCst);
    }
}

#[tokio::test]
async fn the_runs_are_gone_before_the_peer_closes() {
    let (server, _lines) = server();
    let closed_first = Arc::new(AtomicBool::new(true));
    let witness = Witness {
        peer: server.0.peer.clone(),
        closed_first: Arc::clone(&closed_first),
    };
    server.spawn_run(async move {
        let _witness = witness;
        std::future::pending::<()>().await;
    });
    tokio::task::yield_now().await;
    server.stop().await;
    assert!(
        !closed_first.load(Ordering::SeqCst),
        "the peer closed first"
    );
    assert!(server.0.peer.is_closed());
}
