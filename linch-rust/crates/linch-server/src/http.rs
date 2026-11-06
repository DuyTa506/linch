//! HTTP with WebSocket: `GET /rpc` with `Authorization: Bearer <token>`
//! upgrades to a WebSocket that carries one JSON-RPC message per text
//! frame.

use std::future::ready;
use std::io;
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::extract::ws::rejection::WebSocketUpgradeRejection;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use futures::{SinkExt, StreamExt};
use linch::PgPools;
use linch_rpc::{Access, MAX_LINE_BYTES, Tokens};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

/// The one route.
pub const PATH: &str = "/rpc";

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
/// is tracked by `shared.connections` and ends with `stop`.
pub(crate) async fn serve(
    listener: TcpListener,
    shared: Shared,
    stop: CancellationToken,
) -> io::Result<()> {
    let state = Http {
        shared,
        stop: stop.clone(),
    };
    let app = Router::new().route(PATH, get(upgrade)).with_state(state);
    axum::serve(listener, app)
        .with_graceful_shutdown(stop.cancelled_owned())
        .await
}

/// Check the bearer token and take a place among its tenant's
/// connections, then upgrade. The token is checked first, so a request
/// without one learns nothing else; a tenant at its `max_connections`
/// gets 429 instead of a socket that closes at once.
async fn upgrade(
    State(http): State<Http>,
    headers: HeaderMap,
    socket: Result<WebSocketUpgrade, WebSocketUpgradeRejection>,
) -> Response {
    let shared = http.shared;
    let Some(tenant) = bearer(&headers).and_then(|token| shared.tokens.tenant(token)) else {
        return (StatusCode::UNAUTHORIZED, [(WWW_AUTHENTICATE, "Bearer")]).into_response();
    };
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
    socket
        .max_message_size(MAX_MESSAGE_BYTES)
        .on_upgrade(move |socket| async move {
            let _counted = counted;
            serve_socket(socket, Access::Tenant(place), pools, stop).await;
        })
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
