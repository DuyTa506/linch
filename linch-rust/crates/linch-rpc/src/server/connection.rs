//! One connection: messages in from a transport, messages out through one
//! writer task, and the teardown when the input ends.
//!
//! The core sees messages, not bytes: newline-delimited byte streams
//! ([`serve_lines`]) and message transports such as WebSocket
//! ([`serve_messages`]) feed the same loop.

use std::io;
use std::sync::Arc;

use futures::{Sink, SinkExt, Stream, StreamExt};
use linch::{JsonValue, PgPools, RunId};
use serde::Serialize;
use serde_json::value::RawValue;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::mpsc::{self, error::TrySendError};
use tokio_util::codec::{FramedRead, FramedWrite, LinesCodec, LinesCodecError};
use tokio_util::sync::CancellationToken;

use super::outgoing::{Batch, Outgoing};
use super::{Server, login};
use crate::access::Access;
use crate::error::{ErrorObject, code};
use crate::lines::{Frame, HEAD_BYTES, LineCodec};
use crate::message::{Id, Notification, Response};

/// Longest request line the server reads, in bytes.
pub const MAX_LINE_BYTES: usize = 16 * 1024 * 1024;

/// Responses and notifications waiting for the writer.
const OUTBOX_CAPACITY: usize = 1024;

/// Serve one local client until its input ends: [`serve_lines`] with
/// [`Access::Local`], Postgres pools of its own and no stop signal, as the
/// stdio sidecar does (one connection per process).
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
    let pools = Arc::new(PgPools::new());
    serve_lines(
        reader,
        writer,
        Access::Local,
        pools,
        CancellationToken::new(),
    )
    .await
}

/// Serve one client of a byte stream pair, one message per line, until
/// its input ends or `stop` fires.
///
/// Every line of `reader` is one request, or the answer to a server
/// request; every line written to `writer` is one response, notification or
/// server request. When the input ends or `stop` fires the connection
/// stops: each active run is dropped where it stands, as in a crash, so it
/// stays resumable from its last durable state; every server request still
/// waiting fails; every agent's MCP servers are disconnected; queued output
/// is written; then this returns.
///
/// Args:
///     reader: The client's requests.
///     writer: Where responses and notifications go.
///     access: How the client is trusted. With [`Access::Login`] the first
///         line must be `connection.auth`; a refused login, or none within
///         10 s, closes the connection.
///     pools: Where a local client's agents take their Postgres pools;
///         pass one for every connection of a process, so agents on one
///         target share a pool. A tenant's agents take their tenant's own
///         pools instead.
///     stop: Ends the connection as if its input ended.
///
/// Raises:
///     io::Error: If reading the input or writing the output fails.
pub async fn serve_lines<R, W>(
    reader: R,
    writer: W,
    access: Access,
    pools: Arc<PgPools>,
    stop: CancellationToken,
) -> io::Result<()>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let input = FramedRead::new(reader, LineCodec::new(MAX_LINE_BYTES)).map(|frame| match frame {
        Ok(Frame::Line(line)) => match String::from_utf8(line) {
            Ok(line) => Inbound::Message(line),
            Err(_) => Inbound::NotUtf8,
        },
        Ok(Frame::TooLong(head)) => Inbound::TooLong(head),
        Err(error) => Inbound::Failed(error),
    });
    let output = FramedWrite::new(writer, LinesCodec::new());
    let output = SinkExt::<String>::sink_map_err(output, |error| match error {
        LinesCodecError::Io(error) => error,
        other => io::Error::other(other),
    });
    connection(input, output, access, pools, stop).await
}

/// Serve one client of a message transport (one JSON-RPC message per
/// frame, as on a WebSocket); otherwise as [`serve_lines`]. A message
/// over [`MAX_LINE_BYTES`] is refused like an oversized line.
///
/// Args:
///     incoming: The client's messages; an error ends the connection.
///     outgoing: Where responses, notifications and server requests go,
///         one message per item. It is flushed whenever no message waits,
///         and closed at the end.
///     access: How the client is trusted.
///     pools: Where a local client's agents take their Postgres pools; a
///         tenant's agents take their tenant's own.
///     stop: Ends the connection as if its input ended.
///
/// Raises:
///     io::Error: If `incoming` yields an error or `outgoing` fails.
pub async fn serve_messages<S, K>(
    incoming: S,
    outgoing: K,
    access: Access,
    pools: Arc<PgPools>,
    stop: CancellationToken,
) -> io::Result<()>
where
    S: Stream<Item = io::Result<String>> + Unpin,
    K: Sink<String, Error = io::Error> + Unpin + Send + 'static,
{
    let input = incoming.map(|message| match message {
        Ok(message) if message.len() > MAX_LINE_BYTES => {
            let mut cut = HEAD_BYTES;
            while !message.is_char_boundary(cut) {
                cut -= 1;
            }
            Inbound::TooLong(message.as_bytes()[..cut].to_vec())
        }
        Ok(message) => Inbound::Message(message),
        Err(error) => Inbound::Failed(error),
    });
    connection(input, outgoing, access, pools, stop).await
}

/// What a transport delivered.
pub(super) enum Inbound {
    Message(String),
    /// A line that is not UTF-8, already discarded.
    NotUtf8,
    /// A message over [`MAX_LINE_BYTES`], already discarded; its first
    /// bytes.
    TooLong(Vec<u8>),
    Failed(io::Error),
}

async fn connection<S, K>(
    mut input: S,
    output: K,
    access: Access,
    pools: Arc<PgPools>,
    stop: CancellationToken,
) -> io::Result<()>
where
    S: Stream<Item = Inbound> + Unpin,
    K: Sink<String, Error = io::Error> + Unpin + Send + 'static,
{
    let (sender, queue) = mpsc::channel(OUTBOX_CAPACITY);
    let writing = tokio::spawn(write_messages(output, queue));
    let outbox = Outbox(sender);
    // A remote connection holds a place among its tenant's connections
    // until it ends.
    let place = match access {
        Access::Local => None,
        Access::Tenant(place) => Some(place),
        Access::Login(tokens) => match login::login(&mut input, &tokens, &outbox, &stop).await {
            Some(place) => Some(place),
            None => {
                drop(outbox);
                return writing.await.map_err(io::Error::other)?;
            }
        },
    };
    let tenant = place.as_ref().map(|place| Arc::clone(&place.tenant));
    let server = Server::new(outbox, tenant, pools, &stop);
    let mut failed = None;
    loop {
        let inbound = tokio::select! {
            biased;
            () = stop.cancelled() => break,
            inbound = input.next() => inbound,
        };
        match inbound {
            None => break,
            Some(Inbound::Message(message)) => server.receive(&message).await,
            Some(Inbound::NotUtf8) => {
                let error = ErrorObject::new(code::PARSE_ERROR, "not JSON: the line is not UTF-8");
                server.0.outbox.send(&Response::err(Id::Null, error)).await;
            }
            Some(Inbound::TooLong(head)) => server.too_long(&head).await,
            Some(Inbound::Failed(error)) => {
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

/// Write each queued message, joining a run's consecutive envelopes (see
/// [`outgoing`](super::outgoing)), and flush once nothing more waits.
async fn write_messages<K>(mut output: K, mut queue: mpsc::Receiver<Outgoing>) -> io::Result<()>
where
    K: Sink<String, Error = io::Error> + Unpin,
{
    let mut batch = Batch::default();
    while let Some(first) = queue.recv().await {
        let mut next = Some(first);
        while let Some(item) = next {
            match item {
                Outgoing::Message(message) => {
                    if let Some(line) = batch.take() {
                        output.feed(line).await?;
                    }
                    output.feed(message).await?;
                }
                Outgoing::Envelope { run_id, envelope } => {
                    if let Some(line) = batch.push(run_id, envelope) {
                        output.feed(line).await?;
                    }
                }
            }
            next = queue.try_recv().ok();
        }
        if let Some(line) = batch.take() {
            output.feed(line).await?;
        }
        output.flush().await?;
    }
    output.close().await
}

/// Messages for the writer task.
#[derive(Clone)]
pub(super) struct Outbox(pub(super) mpsc::Sender<Outgoing>);

impl Outbox {
    /// Queue `message`; `false` when the writer has stopped (it failed, and
    /// serve reports that).
    pub(super) async fn send<T: Serialize>(&self, message: &T) -> bool {
        (self.0.send(Outgoing::Message(encode(message))).await).is_ok()
    }

    /// Queue `message` without waiting, from code that cannot await (a
    /// drop). When the queue is full a task sends it later.
    pub(super) fn send_now<T: Serialize>(&self, message: &T) {
        match self.0.try_send(Outgoing::Message(encode(message))) {
            Ok(()) | Err(TrySendError::Closed(_)) => {}
            Err(TrySendError::Full(item)) => {
                if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                    let sender = self.0.clone();
                    runtime.spawn(async move { sender.send(item).await });
                }
            }
        }
    }

    pub(super) async fn notify(&self, method: &str, params: JsonValue) {
        self.send(&Notification::new(method, params)).await;
    }

    /// Queue one encoded envelope of `run_id`, for a `run.event`.
    pub(super) async fn envelope(&self, run_id: RunId, envelope: Box<RawValue>) {
        let _ = self.0.send(Outgoing::Envelope { run_id, envelope }).await;
    }
}

fn encode<T: Serialize>(message: &T) -> String {
    // Messages hold only JSON values and strings, which always encode.
    serde_json::to_string(message).expect("a JSON-RPC message encodes")
}
