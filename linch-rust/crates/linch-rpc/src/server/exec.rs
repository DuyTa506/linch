//! The `remote` execution backend's host: every command of the agent runs
//! in the client that opened it, through `exec.start`, `exec.read` and
//! `exec.kill` on its connection.

use std::fmt;
use std::time::Duration;

use async_trait::async_trait;
use linch::{JsonObject, JsonValue, RemoteChunk, RemoteCommand, RemoteError, RemoteExec};
use serde::Serialize;

use super::peer::{CallError, Peer};
use crate::protocol::{ExecKillParams, ExecReadParams, ExecStartParams, method};

/// The host of one agent: its client.
pub(super) struct HostExec {
    agent: String,
    peer: Peer,
}

impl fmt::Debug for HostExec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HostExec")
            .field("agent", &self.agent)
            .finish_non_exhaustive()
    }
}

impl HostExec {
    pub(super) fn new(agent: &str, peer: &Peer) -> Self {
        Self {
            agent: agent.to_owned(),
            peer: peer.clone(),
        }
    }

    /// Send `method` with `params`; the server's own timeouts wrap the
    /// call, and dropping it sends `$/cancelRequest`.
    async fn call(&self, method: &str, params: impl Serialize) -> Result<JsonValue, RemoteError> {
        let JsonValue::Object(params) = serde_json::to_value(params).expect("params encode") else {
            unreachable!("exec params encode as an object");
        };
        let params: JsonObject = params;
        match self.peer.call(method, params, None, None).await {
            Ok(value) => Ok(value),
            Err(CallError::Rpc(error)) => Err(RemoteError::Rejected(error.message)),
            Err(CallError::Protocol(reason)) => Err(RemoteError::Protocol(reason)),
            Err(CallError::Timeout(_)) => Err(RemoteError::Unresponsive),
            Err(CallError::Cancelled) => Err(RemoteError::Unresponsive),
            Err(CallError::Closed) => Err(RemoteError::Closed),
        }
    }

    async fn chunk(
        &self,
        method: &str,
        params: impl Serialize,
    ) -> Result<RemoteChunk, RemoteError> {
        let value = self.call(method, params).await?;
        serde_json::from_value(value).map_err(|error| RemoteError::Protocol(error.to_string()))
    }
}

#[async_trait]
impl RemoteExec for HostExec {
    async fn start(&self, command: RemoteCommand) -> Result<(), RemoteError> {
        let params = ExecStartParams {
            agent: self.agent.clone(),
            exec_id: command.id,
            command: command.command,
            cwd: command.cwd,
            env: command.env,
            background: command.background,
            session_id: command.session,
            principal: command.principal,
            idempotency_key: command.idempotency_key,
        };
        self.call(method::EXEC_START, params).await.map(|_| ())
    }

    async fn read(&self, id: &str, wait: Duration) -> Result<RemoteChunk, RemoteError> {
        let params = ExecReadParams {
            agent: self.agent.clone(),
            exec_id: id.to_owned(),
            wait_ms: u64::try_from(wait.as_millis()).unwrap_or(u64::MAX),
        };
        self.chunk(method::EXEC_READ, params).await
    }

    async fn kill(&self, id: &str) -> Result<RemoteChunk, RemoteError> {
        let params = ExecKillParams {
            agent: self.agent.clone(),
            exec_id: id.to_owned(),
        };
        self.chunk(method::EXEC_KILL, params).await
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;
    use tokio::sync::mpsc;

    use super::super::outgoing::Outgoing;
    use super::*;
    use crate::error::ErrorObject;
    use crate::message::{Id, Response};
    use crate::server::Outbox;

    fn host() -> (HostExec, Peer, mpsc::Receiver<Outgoing>) {
        let (sender, lines) = mpsc::channel(8);
        let peer = Peer::new(Outbox(sender));
        (HostExec::new("agent-1", &peer), peer, lines)
    }

    /// The id of the request the server just sent.
    async fn requested(lines: &mut mpsc::Receiver<Outgoing>) -> (JsonValue, String) {
        let line = lines.recv().await.unwrap().into_line();
        let request: JsonValue = serde_json::from_str(&line).unwrap();
        let id = request["id"].as_str().unwrap().to_owned();
        (request, id)
    }

    /// The connection closing while a read waits is `Closed`: the call ends
    /// interrupted, not with an exit nobody reported.
    #[tokio::test]
    async fn a_connection_that_closes_mid_read_is_closed_not_an_exit() {
        let (host, peer, mut lines) = host();
        let read = tokio::spawn(async move { host.read("exec-1", Duration::from_secs(1)).await });
        let (request, _) = requested(&mut lines).await;
        assert_eq!(request["method"], method::EXEC_READ);
        let params = json!({"agent": "agent-1", "exec_id": "exec-1", "wait_ms": 1000});
        assert_eq!(request["params"], params);
        peer.close();
        assert_eq!(read.await.unwrap(), Err(RemoteError::Closed));
    }

    /// A chunk that is not a `RemoteChunk` is a protocol error, and an error
    /// object keeps the host's message.
    #[tokio::test]
    async fn an_unreadable_chunk_is_a_protocol_error_and_a_host_error_keeps_its_message() {
        for answer in [json!({"exit": {"code": "zero"}}), json!({"stdout": 3})] {
            let (host, peer, mut lines) = host();
            let kill = tokio::spawn(async move { host.kill("exec-1").await });
            let (_, id) = requested(&mut lines).await;
            peer.answer(Response::ok(Id::String(id), answer));
            assert!(matches!(kill.await.unwrap(), Err(RemoteError::Protocol(_))));
        }
        let (host, peer, mut lines) = host();
        let start = tokio::spawn(async move {
            let command = RemoteCommand {
                id: "exec-1".into(),
                command: "true".into(),
                cwd: None,
                env: Default::default(),
                background: false,
                session: "s".into(),
                principal: None,
                idempotency_key: None,
            };
            host.start(command).await
        });
        let (_, id) = requested(&mut lines).await;
        peer.answer(Response::err(
            Id::String(id),
            ErrorObject::new(-32000, "no shell"),
        ));
        assert_eq!(
            start.await.unwrap(),
            Err(RemoteError::Rejected("no shell".into()))
        );
    }
}
