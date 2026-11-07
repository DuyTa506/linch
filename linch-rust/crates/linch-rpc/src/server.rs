//! The server: one connection's agents, runs and calls, fed messages by
//! [`connection`].
//!
//! One read loop parses messages and spawns a task per call, so a slow
//! `agent.open` never holds up `run.cancel`; at most [`MAX_REQUESTS`] calls
//! are in flight, and past that the loop waits. It hands the client's answers
//! to server requests straight to their waiting calls. One writer task owns
//! the output and writes every response, notification and server request
//! from a bounded queue, joining a run's consecutive envelopes into one
//! `run.event` ([`outgoing`]); when the client reads slowly the queue fills
//! and the runs pause, since a run only advances while its stream is
//! polled.

use std::collections::HashMap;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use futures::FutureExt;
use linch::{JsonObject, JsonValue, OpenedAgent, PgPools, RunId, RunStream};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
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
mod login;
mod outgoing;
mod peer;
mod run;

use crate::lines::Head;
use connection::Outbox;
pub use connection::{MAX_LINE_BYTES, serve, serve_lines, serve_messages};
use peer::Peer;

/// Calls one connection may have in flight; the read loop waits for one
/// to finish before it reads past that.
const MAX_REQUESTS: usize = 64;

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
    /// Postgres pools: the tenant's own for a remote client, else the
    /// process's, shared with its other local connections.
    pools: Arc<PgPools>,
    outbox: Outbox,
    /// Requests from the server to the client.
    peer: Peer,
    /// Drops the runs: cancelled at the end of input, or with the
    /// connection's stop token.
    shutdown: CancellationToken,
    /// The tasks driving runs; dropped at shutdown.
    runs: TaskTracker,
    /// The tasks answering calls; they finish at shutdown.
    calls: TaskTracker,
    /// One permit per call in flight.
    permits: Arc<Semaphore>,
}

#[derive(Default)]
struct Tables {
    /// Open agents by handle.
    agents: HashMap<String, Opened>,
    /// Runs this server drives now.
    runs: HashMap<RunId, Driven>,
    /// The places of runs this server drove that suspended with background
    /// workers still running here: each is held until the workers end, or
    /// until an approve or resume of the run takes it over.
    parked: HashMap<RunId, Parked>,
}

/// A suspended run's place, shared with the task that frees it once the
/// run's workers end; whoever takes it first has it.
type Parked = Arc<Mutex<Option<OwnedSemaphorePermit>>>;

/// An agent this connection opened.
struct Opened {
    agent: OpenedAgent,
    /// Its place among the tenant's open agents, freed once the agent is
    /// closed (its MCP servers gone), not when it leaves the table.
    place: Option<OwnedSemaphorePermit>,
}

impl Opened {
    /// Disconnect the agent's MCP servers and wait for them, then free
    /// its place.
    async fn close(self) {
        self.agent.close().await;
        drop(self.place);
    }
}

/// A run this server drives.
struct Driven {
    /// Handle of the agent it runs on.
    agent: String,
    cancel: CancellationToken,
    /// Its place among the tenant's live runs, freed with the entry.
    _place: Option<OwnedSemaphorePermit>,
}

/// What a method produced: a result, or a run to drive after the result
/// `{run_id}` is sent.
enum Reply {
    Value(JsonValue),
    Run(RunStream),
}

impl Server {
    fn new(
        outbox: Outbox,
        tenant: Option<Arc<Tenant>>,
        pools: Arc<PgPools>,
        stop: &CancellationToken,
    ) -> Self {
        Self(Arc::new(State {
            tables: Mutex::default(),
            next_agent: AtomicU64::new(0),
            tenant,
            pools,
            peer: Peer::new(outbox.clone()),
            outbox,
            shutdown: stop.child_token(),
            runs: TaskTracker::new(),
            calls: TaskTracker::new(),
            permits: Arc::new(Semaphore::new(MAX_REQUESTS)),
        }))
    }

    /// The input ended. Calls already read finish and are answered (an
    /// `agent.open` among them registers its agent); runs are dropped
    /// where they stand. Only once both are gone does the peer close, so
    /// no run sees the closed connection as a tool or hook failure. Then
    /// every agent's MCP servers are disconnected and awaited, so no
    /// server process outlives the connection.
    async fn stop(&self) {
        self.0.shutdown.cancel();
        self.0.calls.close();
        self.0.calls.wait().await;
        self.0.runs.close();
        self.0.runs.wait().await;
        self.0.peer.close();
        let agents: Vec<Opened> = (lock(&self.0.tables).agents.drain())
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
        let permits = Arc::clone(&self.0.permits);
        let permit = tokio::select! {
            biased;
            () = self.0.shutdown.cancelled() => return,
            permit = permits.acquire_owned() => permit.expect("the semaphore is never closed"),
        };
        let server = self.clone();
        self.0.calls.spawn(async move {
            server.handle(id, &request.method, request.params).await;
            drop(permit);
        });
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

    /// Drive a run until it ends or the server shuts down.
    fn spawn_run(&self, task: impl Future<Output = ()> + Send + 'static) {
        let shutdown = self.0.shutdown.clone();
        self.0.runs.spawn(async move {
            tokio::select! {
                biased;
                () = shutdown.cancelled() => {}
                () = task => {}
            }
        });
    }

    async fn handle(&self, id: Id, method: &str, params: JsonObject) {
        // A panic answers the call instead of leaving it unanswered.
        let reply = AssertUnwindSafe(self.dispatch(method, params))
            .catch_unwind()
            .await
            .unwrap_or_else(|_| {
                let message = format!("{method} failed inside the server (a panic)");
                Err(ErrorObject::new(code::INTERNAL_ERROR, message))
            });
        match reply {
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
                self.spawn_run(async move { server.drive(stream).await });
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
            method::SESSION_CLOSE => self.close_session(parse(params)?).map(Reply::Value),
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

#[cfg(test)]
mod tests;
