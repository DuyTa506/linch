//! A client: typed calls over any byte stream pair, notifications on a
//! separate receiver, and server requests answered by a
//! [`RequestHandler`].

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use async_trait::async_trait;
use futures::StreamExt;
use linch::{JsonObject, JsonValue};
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use tokio_util::codec::{FramedRead, LinesCodec};
use tokio_util::sync::CancellationToken;

use crate::error::{ErrorObject, code};
use crate::message::{Id, Message, Notification, Request, Response, Version};
use crate::protocol::{CancelRequestParams, method};

/// Bytes buffered each way by [`Client::in_process`].
const PIPE_BYTES: usize = 64 * 1024;

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Response>>>>;

/// Server requests being answered, by id, so `$/cancelRequest` finds them.
type Serving = Arc<Mutex<HashMap<Id, CancellationToken>>>;

/// A call did not produce a result.
#[derive(Debug, thiserror::Error)]
pub enum ClientError {
    /// The server answered with an error.
    #[error(transparent)]
    Rpc(#[from] ErrorObject),
    /// The connection closed before the answer arrived.
    #[error("the connection to the server closed")]
    Closed,
    /// The params did not encode, or the result did not decode as asked.
    #[error("params or result do not fit: {0}")]
    Json(#[from] serde_json::Error),
}

/// Answers the server's requests (`tool.call`, ...) on one connection.
///
/// Each request runs in its own task, so a slow answer holds up nothing
/// else.
#[async_trait]
pub trait RequestHandler: Send + Sync + 'static {
    /// Answer one server request.
    ///
    /// Args:
    ///     method: The request's method, for example `tool.call`.
    ///     params: Its params object.
    ///     cancel: Fires when the server sends `$/cancelRequest` for this
    ///         request or the connection closes. The client then drops this
    ///         future and sends no answer.
    ///
    /// Returns:
    ///     The result object, or the error object to answer with.
    async fn handle(
        &self,
        method: &str,
        params: JsonObject,
        cancel: CancellationToken,
    ) -> Result<JsonValue, ErrorObject>;
}

/// Answers every server request with "method not found".
struct Refuse;

#[async_trait]
impl RequestHandler for Refuse {
    async fn handle(
        &self,
        method: &str,
        _: JsonObject,
        _: CancellationToken,
    ) -> Result<JsonValue, ErrorObject> {
        let message = format!("this client serves no {method:?} requests");
        Err(ErrorObject::new(code::METHOD_NOT_FOUND, message))
    }
}

/// One connection to a server. Calls may run concurrently; answers are
/// matched to calls by id.
///
/// Dropping the client closes its output, which ends the server's input.
pub struct Client {
    outbox: mpsc::Sender<String>,
    pending: Pending,
    next_id: AtomicU64,
}

/// The server's notifications, in the order it sent them. `$/cancelRequest`
/// is handled by the client and not delivered here.
pub struct Notifications(mpsc::UnboundedReceiver<Notification>);

impl Notifications {
    /// The next notification; `None` once the connection has closed.
    pub async fn recv(&mut self) -> Option<Notification> {
        self.0.recv().await
    }
}

impl Client {
    /// Speak to a server that reads `writer` and writes `reader`, refusing
    /// every server request. Same as [`Self::connect_with`] with a handler
    /// that answers "method not found".
    pub fn connect<R, W>(reader: R, writer: W) -> (Self, Notifications)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        Self::connect_with(reader, writer, Arc::new(Refuse))
    }

    /// Speak to a server that reads `writer` and writes `reader`, answering
    /// its requests with `handler`.
    ///
    /// Spawns a reader and a writer task on the current runtime. A line
    /// from the server that is not a valid response, notification or
    /// request closes the connection.
    pub fn connect_with<R, W>(
        reader: R,
        writer: W,
        handler: Arc<dyn RequestHandler>,
    ) -> (Self, Notifications)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let (outbox, queue) = mpsc::channel::<String>(64);
        let (notify, notifications) = mpsc::unbounded_channel();
        let pending = Pending::default();
        tokio::spawn(write_lines(writer, queue));
        let reading = Reader {
            pending: Arc::clone(&pending),
            notify,
            outbox: outbox.downgrade(),
            handler,
            serving: Serving::default(),
            closed: CancellationToken::new(),
        };
        tokio::spawn(reading.run(reader));
        let client = Self {
            outbox,
            pending,
            next_id: AtomicU64::new(1),
        };
        (client, Notifications(notifications))
    }

    /// A server in this process, connected through in-memory pipes. The
    /// server stops when the client is dropped.
    pub fn in_process() -> (Self, Notifications) {
        Self::in_process_with(Arc::new(Refuse))
    }

    /// [`Self::in_process`], answering server requests with `handler`.
    pub fn in_process_with(handler: Arc<dyn RequestHandler>) -> (Self, Notifications) {
        let (ours, theirs) = tokio::io::duplex(PIPE_BYTES);
        let (server_reader, server_writer) = tokio::io::split(theirs);
        tokio::spawn(crate::serve(server_reader, server_writer));
        let (reader, writer) = tokio::io::split(ours);
        Self::connect_with(reader, writer, handler)
    }

    /// Call `method` with `params` and decode its result as `T`.
    ///
    /// Raises:
    ///     ClientError: If the server answers with an error, the connection
    ///         closes first, or the params or result do not fit their types.
    pub async fn call<P, T>(&self, method: &str, params: &P) -> Result<T, ClientError>
    where
        P: Serialize + ?Sized,
        T: DeserializeOwned,
    {
        let params = match serde_json::to_value(params)? {
            JsonValue::Object(params) => params,
            _ => {
                let error = serde::ser::Error::custom("params must be an object");
                return Err(ClientError::Json(error));
            }
        };
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let request = Request {
            jsonrpc: Version::V2,
            id: Some(Id::Number(id.into())),
            method: method.into(),
            params,
        };
        let (answer, answered) = oneshot::channel();
        lock(&self.pending).insert(id, answer);
        let line = serde_json::to_string(&request)?;
        if self.outbox.send(line).await.is_err() {
            lock(&self.pending).remove(&id);
            return Err(ClientError::Closed);
        }
        let response = answered.await.map_err(|_| ClientError::Closed)?;
        Ok(serde_json::from_value(response.into_result()?)?)
    }
}

async fn write_lines<W: AsyncWrite + Unpin>(mut writer: W, mut queue: mpsc::Receiver<String>) {
    while let Some(mut line) = queue.recv().await {
        line.push('\n');
        if writer.write_all(line.as_bytes()).await.is_err() || writer.flush().await.is_err() {
            return;
        }
    }
    // Tell the server the input has ended.
    let _ = writer.shutdown().await;
}

/// The read half of a connection: files responses, notifications and
/// server requests.
struct Reader {
    pending: Pending,
    notify: mpsc::UnboundedSender<Notification>,
    /// Weak, so dropping the client still closes its output.
    outbox: mpsc::WeakSender<String>,
    handler: Arc<dyn RequestHandler>,
    serving: Serving,
    /// Cancelled when the connection closes; every request token is a child.
    closed: CancellationToken,
}

impl Reader {
    async fn run<R: AsyncRead + Unpin>(self, reader: R) {
        let mut lines = FramedRead::new(reader, LinesCodec::new());
        while let Some(Ok(line)) = lines.next().await {
            match serde_json::from_str::<Message>(&line) {
                Ok(Message::Notification(notification))
                    if notification.method == method::CANCEL_REQUEST =>
                {
                    self.cancel(notification.params);
                }
                Ok(Message::Notification(notification)) => {
                    // The caller may have dropped its receiver; that is fine.
                    let _ = self.notify.send(notification);
                }
                Ok(Message::Response(response)) => {
                    let waiter = match &response.id {
                        Id::Number(id) => {
                            id.as_u64().and_then(|id| lock(&self.pending).remove(&id))
                        }
                        _ => None,
                    };
                    if let Some(waiter) = waiter {
                        let _ = waiter.send(response);
                    }
                }
                Ok(Message::Request(request)) => self.serve(request),
                Err(_) => break,
            }
        }
        // Waiting calls see `Closed`; requests being answered are dropped.
        lock(&self.pending).clear();
        self.closed.cancel();
    }

    /// Answer one server request in its own task.
    fn serve(&self, request: Request) {
        // A server request always has an id; without one there is nobody
        // to answer.
        let Some(id) = request.id else { return };
        let cancel = self.closed.child_token();
        lock(&self.serving).insert(id.clone(), cancel.clone());
        let handler = Arc::clone(&self.handler);
        let serving = Arc::clone(&self.serving);
        let outbox = self.outbox.clone();
        tokio::spawn(async move {
            let answer = tokio::select! {
                biased;
                () = cancel.cancelled() => None,
                answer = handler.handle(&request.method, request.params, cancel.clone()) => {
                    Some(answer)
                }
            };
            lock(&serving).remove(&id);
            let response = match answer {
                None => return,
                Some(Ok(result)) => Response::ok(id, result),
                Some(Err(error)) => Response::err(id, error),
            };
            if let (Some(outbox), Ok(line)) = (outbox.upgrade(), serde_json::to_string(&response)) {
                let _ = outbox.send(line).await;
            }
        });
    }

    /// `$/cancelRequest`: cancel the request it names, if still running.
    fn cancel(&self, params: JsonValue) {
        if let Ok(params) = serde_json::from_value::<CancelRequestParams>(params)
            && let Some(token) = lock(&self.serving).remove(&params.id)
        {
            token.cancel();
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code panics while holding these locks.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
