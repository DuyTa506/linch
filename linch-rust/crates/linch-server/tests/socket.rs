//! The Unix socket transport: one listener serves many connections, each
//! must log in with a bearer token first, and stopping ends them all and
//! removes the socket file.

mod common;

use std::time::Duration;

use common::*;
use linch_rpc::protocol::{Authenticated, method};
use linch_rpc::{Client, ClientError, Notifications, code};
use serde_json::{Value, json};
use tokio::net::UnixStream;

async fn connect(path: &std::path::Path) -> (Client, Notifications) {
    let stream = UnixStream::connect(path).await.unwrap();
    let (reader, writer) = stream.into_split();
    Client::connect(reader, writer)
}

async fn login(client: &Client, token: &str) -> Result<Authenticated, ClientError> {
    client
        .call(method::CONNECTION_AUTH, &json!({"token": token}))
        .await
}

#[tokio::test]
async fn the_socket_serves_logged_in_connections_until_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("linch.sock");
    let (server, listening) = running(&config(json!({"socket": path}))).await;
    assert_eq!(listening.socket.as_deref(), Some(path.as_path()));

    let (first, mut first_events) = connect(&path).await;
    assert_eq!(login(&first, TOKEN).await.unwrap().tenant, "acme");
    let (second, mut second_events) = connect(&path).await;
    assert_eq!(login(&second, TOKEN).await.unwrap().tenant, "acme");
    let (one, two) = tokio::join!(
        run(&first, &mut first_events, "one"),
        run(&second, &mut second_events, "two"),
    );
    assert_eq!((one.as_str(), two.as_str()), ("one", "two"));

    server.stop().await;
    assert!(!path.exists(), "the socket file is removed");
    let cancel = json!({"run_id": "r"});
    let after = first.call::<_, Value>(method::RUN_CANCEL, &cancel);
    let after = tokio::time::timeout(Duration::from_secs(5), after).await;
    assert!(matches!(after.expect("no hang"), Err(ClientError::Closed)));
}

#[tokio::test]
async fn a_wrong_or_missing_token_is_refused_and_the_connection_closed() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("linch.sock");
    let (server, _) = running(&config(json!({"socket": path}))).await;

    let (wrong, _events) = connect(&path).await;
    let refused = login(&wrong, "tok-acme-p11c-transport-0002")
        .await
        .unwrap_err();
    assert!(matches!(&refused, ClientError::Rpc(e) if e.code == code::UNAUTHENTICATED));
    assert!(!refused.to_string().contains("transport-0002"), "{refused}");
    let cancel = json!({"run_id": "r"});
    let closed = wrong.call::<_, Value>(method::RUN_CANCEL, &cancel);
    assert!(matches!(closed.await, Err(ClientError::Closed)));

    let (missing, _events) = connect(&path).await;
    let open = scripted("x");
    let skipped = missing.call::<_, Value>(method::AGENT_OPEN, &open);
    assert!(matches!(skipped.await, Err(ClientError::Rpc(e)) if e.code == code::UNAUTHENTICATED));
    server.stop().await;
}

#[tokio::test]
async fn a_stale_socket_file_is_replaced_and_a_live_one_is_not() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("linch.sock");
    drop(std::os::unix::net::UnixListener::bind(&path).unwrap());
    assert!(path.exists());
    let config = config(json!({"socket": path}));
    let (server, _) = running(&config).await;
    let tokens = config.tokens(|_| None).unwrap();
    let busy = linch_server::Listeners::bind(&config, tokens)
        .await
        .unwrap_err();
    assert_eq!(busy.kind(), std::io::ErrorKind::AddrInUse);
    server.stop().await;

    std::fs::write(&path, "not a socket").unwrap();
    let tokens = config.tokens(|_| None).unwrap();
    let error = linch_server::Listeners::bind(&config, tokens)
        .await
        .unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
    assert!(path.exists(), "a regular file is never removed");
}
