//! The server: newline-delimited JSON-RPC over any byte stream pair.
//!
//! One read loop parses lines and spawns a task per call, so a slow
//! `agent.open` never holds up `run.cancel`; it hands the client's answers
//! to server requests straight to their waiting calls. One writer task owns
//! the output and writes every response, notification and server request
//! from a bounded queue; when the client reads slowly the queue fills and
//! the runs pause, since a run only advances while its stream is polled.

use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use futures::StreamExt;
use linch::{JsonObject, JsonValue, OpenedAgent, RunId, RunStream};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc::{self, error::TrySendError};
use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::error::{ErrorObject, code, invalid_params, invalid_request};
use crate::message::{Id, Notification, Request, Response};
use crate::protocol::{RunAccepted, method};

mod agents;
mod callback;
mod host;
mod peer;
mod run;

use peer::Peer;

/// Longest request line the server reads, in bytes.
pub const MAX_LINE_BYTES: usize = 16 * 1024 * 1024;

/// Responses and notifications waiting for the writer.
const OUTBOX_CAPACITY: usize = 1024;

/// Serve one client until its input ends.
///
/// Every line of `reader` is one request, or the answer to a server
/// request; every line written to `writer` is one response, notification or
/// server request. At end of input the server stops: each active run is
/// dropped where it stands, as in a crash, so it stays resumable from its
/// last durable state; every server request still waiting fails; every
/// agent's MCP servers are disconnected; queued output is written; then
/// this returns.
///
/// Args:
///     reader: The client's requests, for example stdin.
///     writer: Where responses and notifications go, for example stdout.
///
/// Raises:
///     io::Error: If reading the input or writing the output fails.
pub async fn serve<R, W>(reader: R, writer: W) -> io::Result<()>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (sender, queue) = mpsc::channel(OUTBOX_CAPACITY);
    let writing = tokio::spawn(write_lines(writer, queue));
    let outbox = Outbox(sender);
    let server = Server(Arc::new(State {
        tables: Mutex::default(),
        next_agent: AtomicU64::new(0),
        peer: Peer::new(outbox.clone()),
        outbox,
        shutdown: CancellationToken::new(),
        tasks: TaskTracker::new(),
    }));
    let mut lines = FramedRead::new(reader, LinesCodec::new_with_max_length(MAX_LINE_BYTES));
    let mut failed = None;
    while let Some(line) = lines.next().await {
        match line {
            Ok(line) => server.receive(&line).await,
            Err(LinesCodecError::MaxLineLengthExceeded) => {
                let error = invalid_request("request line too long");
                server.0.outbox.send(&Response::err(Id::Null, error)).await;
            }
            Err(LinesCodecError::Io(error)) => {
                failed = Some(error);
                break;
            }
        }
    }
    // Drop the runs first, so none sees the closed connection as a tool or
    // hook failure, then fail what still waits on the client.
    server.0.shutdown.cancel();
    server.0.peer.close();
    server.0.tasks.close();
    server.0.tasks.wait().await;
    // Disconnect every agent's MCP servers, so no server process outlives
    // this call.
    let agents: Vec<OpenedAgent> = (lock(&server.0.tables).agents.drain())
        .map(|(_, agent)| agent)
        .collect();
    for agent in agents {
        agent.close().await;
    }
    // The last sender goes with the server; the writer drains and stops.
    drop(server);
    let written = writing.await.map_err(io::Error::other)?;
    match failed {
        Some(error) => Err(error),
        None => written,
    }
}

async fn write_lines<W: AsyncWrite + Unpin>(
    mut writer: W,
    mut queue: mpsc::Receiver<String>,
) -> io::Result<()> {
    while let Some(mut line) = queue.recv().await {
        line.push('\n');
        writer.write_all(line.as_bytes()).await?;
        if queue.is_empty() {
            writer.flush().await?;
        }
    }
    writer.shutdown().await
}

/// Lines for the writer task.
#[derive(Clone)]
struct Outbox(mpsc::Sender<String>);

impl Outbox {
    /// Queue `message`; `false` when the writer has stopped (it failed, and
    /// serve reports that).
    async fn send<T: Serialize>(&self, message: &T) -> bool {
        self.0.send(encode(message)).await.is_ok()
    }

    /// Queue `message` without waiting, from code that cannot await (a
    /// drop). When the queue is full a task sends it later.
    fn send_now<T: Serialize>(&self, message: &T) {
        match self.0.try_send(encode(message)) {
            Ok(()) | Err(TrySendError::Closed(_)) => {}
            Err(TrySendError::Full(line)) => {
                if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                    let sender = self.0.clone();
                    runtime.spawn(async move { sender.send(line).await });
                }
            }
        }
    }

    async fn notify(&self, method: &str, params: JsonValue) {
        self.send(&Notification::new(method, params)).await;
    }
}

fn encode<T: Serialize>(message: &T) -> String {
    // Messages hold only JSON values and strings, which always encode.
    serde_json::to_string(message).expect("a JSON-RPC message encodes")
}

/// Server state shared by every task. Cheap to clone.
#[derive(Clone)]
struct Server(Arc<State>);

struct State {
    /// One lock for both tables, so `agent.close` cannot miss a run that
    /// starts while it checks.
    tables: Mutex<Tables>,
    next_agent: AtomicU64,
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
                let error = invalid_request(&format!("malformed response: {error}"));
                match id {
                    Some(id) => self.0.peer.answer(Response::err(Id::String(id), error)),
                    None => {
                        self.0.outbox.send(&Response::err(Id::Null, error)).await;
                    }
                }
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
