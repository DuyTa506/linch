//! The server: newline-delimited JSON-RPC over any byte stream pair.
//!
//! One read loop parses lines and spawns a task per call, so a slow
//! `agent.open` never holds up `run.cancel`. One writer task owns the
//! output and writes every response and notification from a bounded queue;
//! when the client reads slowly the queue fills and the runs pause, since a
//! run only advances while its stream is polled.

use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use futures::StreamExt;
use linch::{Agent, AgentConfig, JsonObject, JsonValue, RunId, RunStream};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc;
use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::error::{ErrorObject, code, invalid_params, invalid_request};
use crate::message::{Id, Notification, Request, Response};
use crate::protocol::{AgentOpened, Events, EventsParams, RunAccepted, method};

mod run;

/// Longest request line the server reads, in bytes.
pub const MAX_LINE_BYTES: usize = 16 * 1024 * 1024;

/// Responses and notifications waiting for the writer.
const OUTBOX_CAPACITY: usize = 1024;

/// Serve one client until its input ends.
///
/// Every line of `reader` is one request; every line written to `writer`
/// is one response or notification. At end of input the server stops: each
/// active run is dropped where it stands, as in a crash, so it stays
/// resumable from its last durable state; queued output is written; then
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
    let server = Server(Arc::new(State {
        agents: Mutex::new(HashMap::new()),
        runs: Mutex::new(HashMap::new()),
        next_agent: AtomicU64::new(0),
        outbox: Outbox(sender),
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
    server.0.shutdown.cancel();
    server.0.tasks.close();
    server.0.tasks.wait().await;
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
    async fn send<T: Serialize>(&self, message: &T) {
        // Messages hold only JSON values and strings, which always encode.
        let line = serde_json::to_string(message).expect("a JSON-RPC message encodes");
        // A closed queue means the writer failed; serve reports that.
        let _ = self.0.send(line).await;
    }

    async fn notify(&self, method: &str, params: JsonValue) {
        self.send(&Notification::new(method, params)).await;
    }
}

/// Server state shared by every task. Cheap to clone.
#[derive(Clone)]
struct Server(Arc<State>);

struct State {
    agents: Mutex<HashMap<String, Agent>>,
    /// Runs this server drives now, with their cancel tokens.
    runs: Mutex<HashMap<RunId, CancellationToken>>,
    next_agent: AtomicU64,
    outbox: Outbox,
    shutdown: CancellationToken,
    tasks: TaskTracker,
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
                return self.0.outbox.send(&Response::err(Id::Null, error)).await;
            }
        };
        let request = match Request::from_value(value) {
            Ok(request) => request,
            Err(rejected) => return self.0.outbox.send(&rejected).await,
        };
        // This protocol defines no client notifications; JSON-RPC forbids
        // answering one, so it is dropped.
        let Some(id) = request.id else { return };
        let server = self.clone();
        self.spawn(async move { server.handle(id, &request.method, request.params).await });
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
            Ok(Reply::Value(result)) => self.0.outbox.send(&Response::ok(id, result)).await,
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
            Err(error) => self.0.outbox.send(&Response::err(id, error)).await,
        }
    }

    async fn dispatch(&self, name: &str, params: JsonObject) -> Result<Reply, ErrorObject> {
        match name {
            method::AGENT_OPEN => self.open(parse(params)?).await.map(Reply::Value),
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

    async fn open(&self, config: AgentConfig) -> Result<JsonValue, ErrorObject> {
        let agent = config
            .open()
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        let number = self.0.next_agent.fetch_add(1, Ordering::Relaxed) + 1;
        let handle = format!("agent-{number}");
        let contract = agent.contract().to_string();
        lock(&self.0.agents).insert(handle.clone(), agent);
        to_value(&AgentOpened {
            agent: handle,
            contract,
        })
    }

    async fn events(&self, params: EventsParams) -> Result<JsonValue, ErrorObject> {
        let agent = self.agent(&params.agent)?;
        let events = agent
            .events(&params.run_id, params.after_seq)
            .await
            .map_err(|error| ErrorObject::from(&error))?;
        to_value(&Events { events })
    }

    fn agent(&self, handle: &str) -> Result<Agent, ErrorObject> {
        lock(&self.0.agents).get(handle).cloned().ok_or_else(|| {
            ErrorObject::new(code::AGENT_NOT_FOUND, format!("no open agent {handle:?}"))
        })
    }
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
