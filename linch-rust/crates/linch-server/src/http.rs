//! HTTP with WebSocket: `GET /rpc` with `Authorization: Bearer <token>`
//! upgrades to a WebSocket that carries one JSON-RPC message per text
//! frame.

use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use axum::Router;
use axum::extract::State;
use axum::extract::ws::rejection::WebSocketUpgradeRejection;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use futures::stream::SplitSink;
use futures::{SinkExt, StreamExt};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder;
use hyper_util::service::TowerToHyperService;
use linch_rpc::{Access, MAX_LINE_BYTES, Tokens};

use crate::liveness::Liveness;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{Semaphore, mpsc};
use tokio::time::MissedTickBehavior;
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

/// The one route.
pub const PATH: &str = "/rpc";

/// How long a connection not yet upgraded may take to send a whole
/// request head, counted from when the server starts reading it; past it
/// the server closes the connection without an answer (hyper sends none
/// for a timeout), as the socket closes a connection that does not log
/// in within 10 seconds.
pub const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(10);

/// HTTP connections that may wait for their upgrade at once, across the
/// server; one more is closed unread. Each holds a descriptor and up to
/// hyper's ~400 KiB head buffer until it is upgraded or closed, so this
/// bounds them to about 13 MiB; an upgraded WebSocket gives its place
/// back and counts against its tenant's `max_connections` instead.
pub const MAX_PENDING_UPGRADES: usize = 32;

/// The `101` header naming the tenant the token acts for, as the socket's
/// `connection.auth` answer does.
const TENANT: HeaderName = HeaderName::from_static("linch-tenant");

/// Largest WebSocket message read. Messages over [`MAX_LINE_BYTES`] are
/// refused by the core like an oversized line; past this one the socket
/// closes.
const MAX_MESSAGE_BYTES: usize = 4 * MAX_LINE_BYTES;

/// What every HTTP connection of a server shares.
#[derive(Clone)]
pub(crate) struct Shared {
    /// The bearer tokens and their tenants.
    pub(crate) tokens: Arc<Tokens>,
    /// Tracks each upgraded connection, so a stop waits for it.
    pub(crate) connections: TaskTracker,
    /// How a connection that vanished is noticed.
    pub(crate) liveness: Liveness,
}

#[derive(Clone)]
struct Http {
    shared: Shared,
    stop: CancellationToken,
}

/// Serve HTTP on `listener` until `stop` fires; each upgraded connection
/// is tracked by `shared.connections` and ends with `stop`. A connection
/// not yet upgraded holds one of [`MAX_PENDING_UPGRADES`] places, has
/// [`HEADER_READ_TIMEOUT`] for each request head, and is dropped at
/// `stop`.
pub(crate) async fn serve(listener: TcpListener, shared: Shared, stop: CancellationToken) {
    let liveness = shared.liveness;
    let state = Http {
        shared,
        stop: stop.clone(),
    };
    let app = Router::new().route(PATH, get(upgrade)).with_state(state);
    let pending = Arc::new(Semaphore::new(MAX_PENDING_UPGRADES));
    let handshakes = TaskTracker::new();
    loop {
        let accepted = tokio::select! {
            biased;
            () = stop.cancelled() => break,
            accepted = listener.accept() => accepted,
        };
        let stream = match accepted {
            Ok((stream, _)) => stream,
            Err(error) => {
                // Out of descriptors, say: wait a moment rather than spin.
                eprintln!("linch-server: HTTP accept failed: {error}");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        liveness.tune_or_report(&stream);
        // Past the cap the stream is dropped, which closes it unread.
        let Ok(place) = Arc::clone(&pending).try_acquire_owned() else {
            continue;
        };
        let (app, stop) = (app.clone(), stop.clone());
        handshakes.spawn(async move {
            tokio::select! {
                biased;
                () = stop.cancelled() => {}
                () = handshake(stream, app) => {}
            }
            drop(place);
        });
    }
    handshakes.close();
    handshakes.wait().await;
}

/// Serve one connection until it is upgraded or closed: the future ends
/// once the `101` is written and the socket is handed to the upgrade.
async fn handshake(stream: TcpStream, app: Router) {
    let mut builder = Builder::new(TokioExecutor::new());
    // The deadline runs for every head, so a kept-alive connection that
    // falls silent after a refusal closes too.
    builder
        .http1()
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_READ_TIMEOUT);
    let service = TowerToHyperService::new(app);
    let connection = builder.serve_connection_with_upgrades(TokioIo::new(stream), service);
    // A connection's error (a timeout, a bad request) ends only that
    // connection.
    let _ = connection.await;
}

/// Check the bearer token and take a place among its tenant's
/// connections, then upgrade, naming the tenant in `Linch-Tenant`. The
/// token is checked first, so a request without one learns nothing else;
/// a tenant at its `max_connections` gets 429 instead of a socket that
/// closes at once.
async fn upgrade(
    State(http): State<Http>,
    headers: HeaderMap,
    socket: Result<WebSocketUpgrade, WebSocketUpgradeRejection>,
) -> Response {
    let shared = http.shared;
    let Some(tenant) = bearer(&headers).and_then(|token| shared.tokens.tenant(token)) else {
        return (StatusCode::UNAUTHORIZED, [(WWW_AUTHENTICATE, "Bearer")]).into_response();
    };
    // `Tenant::new` allows only ASCII letters, digits, `.`, `_` and `-`.
    let name = HeaderValue::from_str(tenant.name()).expect("a tenant name is a header value");
    let place = match tenant.connect() {
        Ok(place) => place,
        Err(error) => return (StatusCode::TOO_MANY_REQUESTS, error.message).into_response(),
    };
    let socket = match socket {
        Ok(socket) => socket,
        Err(rejection) => return rejection.into_response(),
    };
    // Counted from now, so a stop cannot finish before this connection.
    let counted = shared.connections.token();
    let stop = http.stop;
    let liveness = shared.liveness;
    let mut switching =
        socket
            .max_message_size(MAX_MESSAGE_BYTES)
            .on_upgrade(move |socket| async move {
                let _counted = counted;
                serve_socket(socket, Access::Tenant(place), stop, liveness).await;
            });
    switching.headers_mut().insert(TENANT, name);
    switching
}

/// The token of an `Authorization: Bearer <token>` header.
fn bearer(headers: &HeaderMap) -> Option<&str> {
    let value = headers.get(AUTHORIZATION)?.to_str().ok()?;
    let (scheme, token) = value.split_once(' ')?;
    scheme.eq_ignore_ascii_case("bearer").then(|| token.trim())
}

/// What the WebSocket's reader and the pinger of one connection share.
#[derive(Default)]
struct Pulse {
    /// When a frame (of any kind) last came in.
    heard: Mutex<Option<Instant>>,
    /// Whether the reader holds a message the connection has no room for
    /// yet (it is busy with 64 calls): the client is alive and sending, and
    /// the pongs it answers sit unread behind that message.
    backed_up: AtomicBool,
}

impl Pulse {
    fn touch(&self) {
        *self.heard.lock().unwrap_or_else(PoisonError::into_inner) = Some(Instant::now());
    }

    /// How long nothing has come in; zero while the reader is backed up.
    fn silence(&self, since: Instant) -> Duration {
        if self.backed_up.load(Ordering::Acquire) {
            return Duration::ZERO;
        }
        let heard = *self.heard.lock().unwrap_or_else(PoisonError::into_inner);
        heard.unwrap_or(since).elapsed()
    }
}

/// Serve one WebSocket. A ping goes out every `liveness.ping_interval`, and
/// a socket that has sent nothing for `liveness.pong_timeout` (not a frame,
/// not a pong) is ended as if its input had ended, which frees its
/// tenant's place, its workflow hold and its calls.
///
/// The frames are read by a loop of their own that hands each message over
/// through a channel of one: the connection stops reading input while it has
/// 64 calls in flight, and a pong the connection never read would read as a
/// dead client.
async fn serve_socket(socket: WebSocket, access: Access, stop: CancellationToken, live: Liveness) {
    let started = Instant::now();
    let (sink, mut frames) = socket.split();
    let sink = Arc::new(tokio::sync::Mutex::new(sink));
    let pulse = Arc::new(Pulse::default());
    let (messages, incoming) = mpsc::channel::<io::Result<String>>(1);
    let reading = {
        let pulse = Arc::clone(&pulse);
        async move {
            while let Some(frame) = frames.next().await {
                pulse.touch();
                let message = match frame {
                    Ok(Message::Text(text)) => Ok(text.to_string()),
                    Ok(Message::Binary(bytes)) => String::from_utf8(bytes.to_vec()).map_err(|_| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "a binary frame that is not UTF-8",
                        )
                    }),
                    // Pings are answered by the socket itself; a close ends the stream.
                    Ok(Message::Ping(_) | Message::Pong(_)) => continue,
                    Ok(Message::Close(_)) => break,
                    Err(error) => Err(io::Error::other(error)),
                };
                let failed = message.is_err();
                match messages.try_send(message) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(message)) => {
                        pulse.backed_up.store(true, Ordering::Release);
                        let sent = messages.send(message).await;
                        pulse.backed_up.store(false, Ordering::Release);
                        pulse.touch();
                        if sent.is_err() {
                            break;
                        }
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => break,
                }
                if failed {
                    break;
                }
            }
            // Dropping the sender ends the connection's input.
        }
    };
    let incoming = Box::pin(futures::stream::unfold(
        incoming,
        |mut incoming| async move { incoming.recv().await.map(|message| (message, incoming)) },
    ));
    let outgoing = futures::sink::unfold(Arc::clone(&sink), |sink, message: String| async move {
        let sent = sink.lock().await.send(Message::Text(message.into())).await;
        sent.map(|()| sink).map_err(io::Error::other)
    });
    let ended = stop.child_token();
    let closing = Arc::clone(&sink);
    let pinging = ping(sink, pulse, started, live, ended.clone());
    let serving = linch_rpc::serve_messages(incoming, Box::pin(outgoing), access, ended);
    // A connection's I/O error ends only that connection.
    tokio::select! {
        _ = serving => {}
        () = pinging => {}
        // When the client closes, the input ends and the connection finishes
        // its calls; it is not cut short.
        () = async { reading.await; std::future::pending::<()>().await } => {}
    }
    // Say goodbye if the peer is still there to hear it.
    let close = async { closing.lock().await.close().await };
    let _ = tokio::time::timeout(Duration::from_secs(5), close).await;
}

/// Ping the client every `ping_interval` until it falls silent for
/// `pong_timeout`, or a ping cannot be sent in that time (the client reads
/// nothing and the socket's buffers are full); then end the connection.
async fn ping(
    sink: Arc<tokio::sync::Mutex<SplitSink<WebSocket, Message>>>,
    pulse: Arc<Pulse>,
    started: Instant,
    live: Liveness,
    ended: CancellationToken,
) {
    let mut tick = tokio::time::interval(live.ping_interval);
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        // Wake for the next ping, or when the silence reaches the timeout,
        // whichever comes first: a client is dropped `pong_timeout` after its
        // last frame, not at the next ping after that.
        let timeout_in = live.pong_timeout.saturating_sub(pulse.silence(started));
        let due = tokio::select! {
            _ = tick.tick() => true,
            () = tokio::time::sleep(timeout_in) => false,
        };
        let ping_failed = async {
            let sent = tokio::time::timeout(live.pong_timeout, async {
                sink.lock()
                    .await
                    .send(Message::Ping(Vec::new().into()))
                    .await
            });
            !matches!(sent.await, Ok(Ok(())))
        };
        if pulse.silence(started) >= live.pong_timeout || (due && ping_failed.await) {
            ended.cancel();
            // The connection ends on `ended`; wait for it to drop this task.
            std::future::pending::<()>().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A reader holding a message the connection has no room for is alive and
    /// sending, so its silence is zero however long ago a frame was read.
    #[test]
    fn a_backed_up_reader_is_never_silent() {
        let pulse = Pulse::default();
        let long_ago = Instant::now() - Duration::from_secs(3600);
        assert!(pulse.silence(long_ago) >= Duration::from_secs(3600));
        pulse.backed_up.store(true, Ordering::Release);
        assert_eq!(pulse.silence(long_ago), Duration::ZERO);
        pulse.backed_up.store(false, Ordering::Release);
        pulse.touch();
        assert!(pulse.silence(long_ago) < Duration::from_secs(1));
    }
}
