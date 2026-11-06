//! A client: typed calls over any byte stream pair, with notifications on
//! a separate receiver.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use futures::StreamExt;
use linch::JsonValue;
use serde::Serialize;
use serde::de::DeserializeOwned;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use tokio_util::codec::{FramedRead, LinesCodec};

use crate::error::ErrorObject;
use crate::message::{Id, Message, Notification, Request, Response, Version};

/// Bytes buffered each way by [`Client::in_process`].
const PIPE_BYTES: usize = 64 * 1024;

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Response>>>>;

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

/// One connection to a server. Calls may run concurrently; answers are
/// matched to calls by id.
///
/// Dropping the client closes its output, which ends the server's input.
pub struct Client {
    outbox: mpsc::Sender<String>,
    pending: Pending,
    next_id: AtomicU64,
}

/// The server's notifications, in the order it sent them.
pub struct Notifications(mpsc::UnboundedReceiver<Notification>);

impl Notifications {
    /// The next notification; `None` once the connection has closed.
    pub async fn recv(&mut self) -> Option<Notification> {
        self.0.recv().await
    }
}

impl Client {
    /// Speak to a server that reads `writer` and writes `reader`.
    ///
    /// Spawns a reader and a writer task on the current runtime. A line
    /// from the server that is not a valid response or notification closes
    /// the connection.
    pub fn connect<R, W>(reader: R, writer: W) -> (Self, Notifications)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let (outbox, queue) = mpsc::channel::<String>(64);
        let (notify, notifications) = mpsc::unbounded_channel();
        let pending = Pending::default();
        tokio::spawn(write_lines(writer, queue));
        tokio::spawn(read_lines(reader, Arc::clone(&pending), notify));
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
        let (ours, theirs) = tokio::io::duplex(PIPE_BYTES);
        let (server_reader, server_writer) = tokio::io::split(theirs);
        tokio::spawn(crate::serve(server_reader, server_writer));
        let (reader, writer) = tokio::io::split(ours);
        Self::connect(reader, writer)
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

async fn read_lines<R: AsyncRead + Unpin>(
    reader: R,
    pending: Pending,
    notify: mpsc::UnboundedSender<Notification>,
) {
    let mut lines = FramedRead::new(reader, LinesCodec::new());
    while let Some(Ok(line)) = lines.next().await {
        match serde_json::from_str::<Message>(&line) {
            Ok(Message::Notification(notification)) => {
                // The caller may have dropped its receiver; that is fine.
                let _ = notify.send(notification);
            }
            Ok(Message::Response(response)) => {
                let waiter = match &response.id {
                    Id::Number(id) => id.as_u64().and_then(|id| lock(&pending).remove(&id)),
                    _ => None,
                };
                if let Some(waiter) = waiter {
                    let _ = waiter.send(response);
                }
            }
            Err(_) => break,
        }
    }
    // Waiting calls see `Closed`.
    lock(&pending).clear();
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code panics while holding the lock.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
