//! HTTP with WebSocket: `GET /rpc` with `Authorization: Bearer <token>`
//! upgrades to a WebSocket that carries one JSON-RPC message per text
//! frame.

use std::future::ready;
use std::io;
use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::extract::State;
use axum::extract::ws::rejection::WebSocketUpgradeRejection;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use futures::{SinkExt, StreamExt};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder;
use hyper_util::service::TowerToHyperService;
use linch::PgPools;
use linch_rpc::{Access, MAX_LINE_BYTES, Tokens};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;
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
    /// The Postgres pools of the server's agents.
    pub(crate) pools: Arc<PgPools>,
    /// Tracks each upgraded connection, so a stop waits for it.
    pub(crate) connections: TaskTracker,
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
    let (pools, stop) = (shared.pools, http.stop);
    let mut switching =
        socket
            .max_message_size(MAX_MESSAGE_BYTES)
            .on_upgrade(move |socket| async move {
                let _counted = counted;
                serve_socket(socket, Access::Tenant(place), pools, stop).await;
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

async fn serve_socket(
    socket: WebSocket,
    access: Access,
    pools: Arc<PgPools>,
    stop: CancellationToken,
) {
    let (sink, stream) = socket.split();
    let incoming = stream.filter_map(|frame| {
        ready(match frame {
            Ok(Message::Text(text)) => Some(Ok(text.to_string())),
            Ok(Message::Binary(bytes)) => Some(String::from_utf8(bytes.to_vec()).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "a binary frame that is not UTF-8",
                )
            })),
            // Pings are answered by the socket itself; a close ends the stream.
            Ok(Message::Ping(_) | Message::Pong(_) | Message::Close(_)) => None,
            Err(error) => Some(Err(io::Error::other(error))),
        })
    });
    let outgoing = sink
        .with(|message: String| ready(Ok::<_, axum::Error>(Message::Text(message.into()))))
        .sink_map_err(io::Error::other);
    // A connection's I/O error ends only that connection.
    let _ = linch_rpc::serve_messages(incoming, Box::pin(outgoing), access, pools, stop).await;
}
