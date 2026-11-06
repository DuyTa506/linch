//! The HTTP transport: `GET /rpc` with a bearer token upgrades to a
//! WebSocket that carries one JSON-RPC message per text frame; a missing
//! or wrong token gets 401 before any upgrade. Its connections and the
//! socket's share the server's Postgres pools.

mod common;

use std::net::SocketAddr;
use std::time::Duration;

use common::*;
use futures::{SinkExt, StreamExt};
use linch_rpc::Client;
use linch_server::ServerConfig;
use serde_json::{Value, json};
use tokio::net::{TcpStream, UnixStream};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::StatusCode;
use tokio_tungstenite::tungstenite::{Error, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

async fn connect(address: SocketAddr, authorization: Option<&str>) -> Result<Socket, Error> {
    let mut request = format!("ws://{address}{}", linch_server::PATH)
        .into_client_request()
        .unwrap();
    if let Some(value) = authorization {
        request
            .headers_mut()
            .insert("authorization", value.parse().unwrap());
    }
    tokio_tungstenite::connect_async(request)
        .await
        .map(|(socket, _)| socket)
}

async fn send(socket: &mut Socket, id: u64, method: &str, params: Value) {
    let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
    socket
        .send(Message::Text(request.to_string().into()))
        .await
        .unwrap();
}

async fn recv(socket: &mut Socket) -> Option<Value> {
    loop {
        let frame = tokio::time::timeout(Duration::from_secs(10), socket.next()).await;
        match frame.expect("server silent")? {
            Ok(Message::Text(text)) => return Some(serde_json::from_str(&text).unwrap()),
            Ok(Message::Close(_)) | Err(_) => return None,
            Ok(_) => {}
        }
    }
}

#[tokio::test]
async fn a_websocket_with_a_valid_token_runs_an_agent() {
    let (server, listening) = running(&config(json!({"http": "127.0.0.1:0"}))).await;
    let address = listening.http.unwrap();
    let mut socket = connect(address, Some(&format!("Bearer {TOKEN}")))
        .await
        .unwrap();
    send(&mut socket, 1, "agent.open", scripted("over websocket")).await;
    let opened = recv(&mut socket).await.unwrap();
    let agent = opened["result"]["agent"].clone();
    assert!(agent.is_string(), "{opened}");
    let start = json!({"agent": agent, "session_id": "s1", "prompt": "hi"});
    send(&mut socket, 2, "run.start", start).await;
    assert!(recv(&mut socket).await.unwrap()["result"]["run_id"].is_string());
    let mut last = Value::Null;
    loop {
        let message = recv(&mut socket).await.unwrap();
        match message["method"].as_str() {
            Some("run.event") => last = message["params"]["event"].clone(),
            Some("run.end") => break,
            _ => panic!("unexpected {message}"),
        }
    }
    assert_eq!(last["final_text"], "over websocket", "{last}");
    // The token's tenant bounds the namespace here too.
    let mut other = scripted("x");
    other["namespace"] = json!("beta");
    send(&mut socket, 3, "agent.open", other).await;
    let refused = recv(&mut socket).await.unwrap();
    assert_eq!(refused["error"]["code"], linch_rpc::code::NOT_ALLOWED);
    // Login is the socket's business only.
    send(&mut socket, 4, "connection.auth", json!({"token": TOKEN})).await;
    let again = recv(&mut socket).await.unwrap();
    assert_eq!(again["error"]["code"], linch_rpc::code::INVALID_REQUEST);

    server.stop().await;
    assert!(recv(&mut socket).await.is_none(), "stop ends the socket");
}

#[tokio::test]
async fn a_websocket_and_a_socket_connection_share_a_postgres_pool() {
    let Some(db) = common::pg::TestDb::empty("server_pools").await else {
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("linch.sock");
    let config = ServerConfig::from_json(
        &json!({"socket": path, "http": "127.0.0.1:0",
                "tenants": {"acme": {"tokens": [{"value": TOKEN}],
                                     "allow": {"scripted_provider": true,
                                               "postgres_dsns": [db.dsn]}}}})
        .to_string(),
    )
    .unwrap();
    let (server, listening) = running(&config).await;
    let mut agent = scripted("x");
    agent["stores"] = json!({"type": "postgres", "dsn": db.dsn});

    let mut socket = connect(listening.http.unwrap(), Some(&format!("Bearer {TOKEN}")))
        .await
        .unwrap();
    send(&mut socket, 1, "agent.open", agent.clone()).await;
    let opened = recv(&mut socket).await.unwrap();
    assert!(opened["result"]["agent"].is_string(), "{opened}");
    let stream = UnixStream::connect(&path).await.unwrap();
    let (reader, writer) = stream.into_split();
    let (client, _events) = Client::connect(reader, writer);
    let auth = json!({"token": TOKEN});
    let _: Value = client.call("connection.auth", &auth).await.unwrap();
    let opened: Value = client.call("agent.open", &agent).await.unwrap();
    assert!(opened["agent"].is_string(), "{opened}");
    assert_eq!(db.connections().await, 1);

    server.stop().await;
    db.drop().await;
}

#[tokio::test]
async fn a_missing_or_wrong_token_gets_401_before_the_upgrade() {
    let (server, listening) = running(&config(json!({"http": "127.0.0.1:0"}))).await;
    let address = listening.http.unwrap();
    let wrong = format!("Bearer {TOKEN}x");
    for authorization in [
        None,
        Some(wrong.as_str()),
        Some(TOKEN),
        Some("Basic YWNtZQ=="),
    ] {
        match connect(address, authorization).await {
            Err(Error::Http(response)) => {
                assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
                assert_eq!(response.headers()["www-authenticate"], "Bearer");
                let body = response.body().as_deref().unwrap_or_default();
                assert!(!String::from_utf8_lossy(body).contains(TOKEN));
            }
            other => panic!("{authorization:?} was not refused: {:?}", other.map(|_| ())),
        }
    }
    // The scheme is case-insensitive.
    let lower = format!("bearer {TOKEN}");
    assert!(connect(address, Some(&lower)).await.is_ok());
    server.stop().await;
}
