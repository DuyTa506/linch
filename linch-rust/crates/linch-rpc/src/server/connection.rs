//! One connection: messages in from a transport, messages out through one
//! writer task, and the teardown when the input ends.
//!
//! The core sees messages, not bytes: newline-delimited byte streams
//! ([`serve`]) and message transports such as WebSocket
//! ([`serve_messages`]) feed the same loop.

use std::io;

use futures::{Sink, SinkExt, Stream, StreamExt};
use linch::JsonValue;
use serde::Serialize;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::mpsc::{self, error::TrySendError};
use tokio_util::codec::{FramedRead, FramedWrite, LinesCodec, LinesCodecError};

use super::Server;
use crate::error::invalid_request;
use crate::message::{Id, Notification, Response};

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
    let input =
        FramedRead::new(reader, LinesCodec::new_with_max_length(MAX_LINE_BYTES)).map(|line| {
            match line {
                Ok(line) => Inbound::Message(line),
                Err(LinesCodecError::MaxLineLengthExceeded) => Inbound::TooLong,
                Err(LinesCodecError::Io(error)) => Inbound::Failed(error),
            }
        });
    let output = FramedWrite::new(writer, LinesCodec::new());
    let output = SinkExt::<String>::sink_map_err(output, |error| match error {
        LinesCodecError::Io(error) => error,
        other => io::Error::other(other),
    });
    connection(input, output).await
}

/// Serve one client of a message transport (one JSON-RPC message per
/// frame, as on a WebSocket) until its input ends; otherwise as [`serve`].
///
/// Args:
///     incoming: The client's messages; an error ends the connection.
///     outgoing: Where responses, notifications and server requests go,
///         one message per item. It is flushed whenever no message waits,
///         and closed at the end.
///
/// Raises:
///     io::Error: If `incoming` yields an error or `outgoing` fails.
pub async fn serve_messages<S, K>(incoming: S, outgoing: K) -> io::Result<()>
where
    S: Stream<Item = io::Result<String>> + Unpin,
    K: Sink<String, Error = io::Error> + Unpin + Send + 'static,
{
    let input = incoming.map(|message| match message {
        Ok(message) => Inbound::Message(message),
        Err(error) => Inbound::Failed(error),
    });
    connection(input, outgoing).await
}

/// What a transport delivered.
enum Inbound {
    Message(String),
    /// A line over [`MAX_LINE_BYTES`], already discarded.
    TooLong,
    Failed(io::Error),
}

async fn connection<S, K>(mut input: S, output: K) -> io::Result<()>
where
    S: Stream<Item = Inbound> + Unpin,
    K: Sink<String, Error = io::Error> + Unpin + Send + 'static,
{
    let (sender, queue) = mpsc::channel(OUTBOX_CAPACITY);
    let writing = tokio::spawn(write_messages(output, queue));
    let server = Server::new(Outbox(sender));
    let mut failed = None;
    while let Some(inbound) = input.next().await {
        match inbound {
            Inbound::Message(message) => server.receive(&message).await,
            Inbound::TooLong => {
                let error = invalid_request("request line too long");
                server.0.outbox.send(&Response::err(Id::Null, error)).await;
            }
            Inbound::Failed(error) => {
                failed = Some(error);
                break;
            }
        }
    }
    server.stop().await;
    // The last sender goes with the server; the writer drains and stops.
    drop(server);
    let written = writing.await.map_err(io::Error::other)?;
    match failed {
        Some(error) => Err(error),
        None => written,
    }
}

async fn write_messages<K>(mut output: K, mut queue: mpsc::Receiver<String>) -> io::Result<()>
where
    K: Sink<String, Error = io::Error> + Unpin,
{
    while let Some(message) = queue.recv().await {
        output.feed(message).await?;
        if queue.is_empty() {
            output.flush().await?;
        }
    }
    output.close().await
}

/// Messages for the writer task.
#[derive(Clone)]
pub(super) struct Outbox(pub(super) mpsc::Sender<String>);

impl Outbox {
    /// Queue `message`; `false` when the writer has stopped (it failed, and
    /// serve reports that).
    pub(super) async fn send<T: Serialize>(&self, message: &T) -> bool {
        self.0.send(encode(message)).await.is_ok()
    }

    /// Queue `message` without waiting, from code that cannot await (a
    /// drop). When the queue is full a task sends it later.
    pub(super) fn send_now<T: Serialize>(&self, message: &T) {
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

    pub(super) async fn notify(&self, method: &str, params: JsonValue) {
        self.send(&Notification::new(method, params)).await;
    }
}

fn encode<T: Serialize>(message: &T) -> String {
    // Messages hold only JSON values and strings, which always encode.
    serde_json::to_string(message).expect("a JSON-RPC message encodes")
}
