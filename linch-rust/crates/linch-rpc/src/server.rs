//! The server: one connection's agents, runs and calls, fed messages by
//! [`connection`].
//!
//! One read loop parses messages and spawns a task per call, so a slow
//! `agent.open` never holds up `run.cancel`; it hands the client's answers
//! to server requests straight to their waiting calls. One writer task owns
//! the output and writes every response, notification and server request
//! from a bounded queue; when the client reads slowly the queue fills and
//! the runs pause, since a run only advances while its stream is polled.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use linch::{JsonObject, JsonValue, OpenedAgent, RunId, RunStream};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::access::Tenant;
use crate::error::{ErrorObject, code, invalid_params, invalid_request};
use crate::message::{Id, Request, Response};
use crate::protocol::{RunAccepted, method};

mod agents;
mod callback;
mod connection;
mod host;
mod lines;
mod login;
mod peer;
mod run;

use connection::Outbox;
pub use connection::{MAX_LINE_BYTES, serve, serve_lines, serve_messages};
use lines::Head;
use peer::Peer;

/// Server state shared by every task. Cheap to clone.
#[derive(Clone)]
struct Server(Arc<State>);

struct State {
    /// One lock for both tables, so `agent.close` cannot miss a run that
    /// starts while it checks.
    tables: Mutex<Tables>,
    next_agent: AtomicU64,
    /// The tenant a remote client acts for; `None` for a local client,
    /// which is not checked.
    tenant: Option<Arc<Tenant>>,
    outbox: Outbox,
    /// Requests from the server to the client.
    peer: Peer,
    shutdown: CancellationToken,
    tasks: TaskTracker,
}

#[derive(Default)]
struct Tables {
    /// Open agents by handle.
    agents: HashMap<String, OpenedAgent>,
    /// Runs this server drives now.
    runs: HashMap<RunId, Driven>,
}

/// A run this server drives.
struct Driven {
    /// Handle of the agent it runs on.
    agent: String,
    cancel: CancellationToken,
}

/// What a method produced: a result, or a run to drive after the result
/// `{run_id}` is sent.
enum Reply {
    Value(JsonValue),
    Run(RunStream),
}

impl Server {
    fn new(outbox: Outbox, tenant: Option<Arc<Tenant>>) -> Self {
        Self(Arc::new(State {
            tables: Mutex::default(),
            next_agent: AtomicU64::new(0),
            tenant,
            peer: Peer::new(outbox.clone()),
            outbox,
            shutdown: CancellationToken::new(),
            tasks: TaskTracker::new(),
        }))
    }

    /// The input ended: drop the runs first, so none sees the closed
    /// connection as a tool or hook failure, then fail what still waits on
    /// the client, then disconnect every agent's MCP servers, so no server
    /// process outlives the connection.
    async fn stop(&self) {
        self.0.shutdown.cancel();
        self.0.peer.close();
        self.0.tasks.close();
        self.0.tasks.wait().await;
        let agents: Vec<OpenedAgent> = (lock(&self.0.tables).agents.drain())
            .map(|(_, agent)| agent)
            .collect();
        for agent in agents {
            agent.close().await;
        }
    }

    async fn receive(&self, line: &str) {
        if line.trim().is_empty() {
            return;
        }
        let value = match serde_json::from_str::<JsonValue>(line) {
            Ok(value) => value,
            Err(error) => {
                let error = ErrorObject::new(code::PARSE_ERROR, format!("not JSON: {error}"));
                self.0.outbox.send(&Response::err(Id::Null, error)).await;
                return;
            }
        };
        if is_response(&value) {
            return self.answer(value).await;
        }
        let request = match Request::from_value(value) {
            Ok(request) => request,
            Err(rejected) => {
                self.0.outbox.send(&rejected).await;
                return;
            }
        };
        // This protocol defines no client notifications; JSON-RPC forbids
        // answering one, so it is dropped.
        let Some(id) = request.id else { return };
        let server = self.clone();
        self.spawn(async move { server.handle(id, &request.method, request.params).await });
    }

    /// Hand the client's answer to the server request it names. A
    /// malformed answer fails that request; with no readable id it gets a
    /// `-32600` error line, as any unreadable message does.
    async fn answer(&self, value: JsonValue) {
        let id = value
            .get("id")
            .and_then(JsonValue::as_str)
            .map(str::to_owned);
        match serde_json::from_value::<Response>(value) {
            Ok(response) => self.0.peer.answer(response),
            Err(error) => {
                let message = format!("malformed response: {error}");
                match id {
                    Some(id) => self.0.peer.malformed(&id, message),
                    None => {
                        let error = invalid_request(&message);
                        self.0.outbox.send(&Response::err(Id::Null, error)).await;
                    }
                }
            }
        }
    }

    /// A message over the size limit, of which only `head` was kept. An
    /// answer to a server request fails that request; a request is
    /// refused under its id when the head shows it.
    async fn too_long(&self, head: &[u8]) {
        let head = Head::read(head);
        let message = format!("message longer than {MAX_LINE_BYTES} bytes");
        match head.id {
            Some(Id::String(id)) if head.answer && !head.method => {
                self.0.peer.malformed(&id, message);
            }
            Some(id) if !head.answer => {
                let error = invalid_request(&message);
                self.0.outbox.send(&Response::err(id, error)).await;
            }
            _ => {
                let error = invalid_request(&message);
                self.0.outbox.send(&Response::err(Id::Null, error)).await;
            }
        }
    }

    /// Run `task` until it ends or the server shuts down.
    fn spawn(&self, task: impl Future<Output = ()> + Send + 'static) {
        let shutdown = self.0.shutdown.clone();
        self.0.tasks.spawn(async move {
            tokio::select! {
                biased;
                () = shutdown.cancelled() => {}
                () = task => {}
            }
        });
    }

    async fn handle(&self, id: Id, method: &str, params: JsonObject) {
        match self.dispatch(method, params).await {
            Ok(Reply::Value(result)) => {
                self.0.outbox.send(&Response::ok(id, result)).await;
            }
            Ok(Reply::Run(stream)) => {
                let accepted = RunAccepted {
                    run_id: stream.run_id().clone(),
                };
                // The result goes out before the run's first notification.
                let result = serde_json::to_value(&accepted).expect("a run id encodes");
                self.0.outbox.send(&Response::ok(id, result)).await;
                let server = self.clone();
                self.spawn(async move { server.drive(stream).await });
            }
            Err(error) => {
                self.0.outbox.send(&Response::err(id, error)).await;
            }
        }
    }

    async fn dispatch(&self, name: &str, params: JsonObject) -> Result<Reply, ErrorObject> {
        match name {
            method::AGENT_OPEN => self.open(parse(params)?).await.map(Reply::Value),
            method::AGENT_CLOSE => self.close(parse(params)?).await.map(Reply::Value),
            method::RUN_START => self.start(parse(params)?),
            method::RUN_RESUME => self.resume(parse(params)?),
            method::RUN_APPROVE => self.approve(parse(params)?),
            method::RUN_CANCEL => to_value(&self.cancel(parse(params)?)).map(Reply::Value),
            method::RUN_EVENTS => self.events(parse(params)?).await.map(Reply::Value),
            method::CONNECTION_AUTH => Err(invalid_request(
                "connection.auth is only accepted as the first message of a socket connection",
            )),
            other => Err(ErrorObject::new(
                code::METHOD_NOT_FOUND,
                format!("no method {other:?}"),
            )),
        }
    }
}

/// An object with no `method` but a `result` or `error` answers a server
/// request; anything else is read as a request.
fn is_response(value: &JsonValue) -> bool {
    value.as_object().is_some_and(|object| {
        !object.contains_key("method")
            && (object.contains_key("result") || object.contains_key("error"))
    })
}

fn parse<P: DeserializeOwned>(params: JsonObject) -> Result<P, ErrorObject> {
    serde_json::from_value(JsonValue::Object(params))
        .map_err(|error| invalid_params(&error.to_string()))
}

fn to_value<T: Serialize>(value: &T) -> Result<JsonValue, ErrorObject> {
    serde_json::to_value(value).map_err(|error| {
        ErrorObject::new(
            code::INTERNAL_ERROR,
            format!("result could not be encoded: {error}"),
        )
    })
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code panics while holding these locks.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
