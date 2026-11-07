//! The client's side of server requests, over a raw wire: answers, errors,
//! `$/cancelRequest`, the default refusal, and a closed connection.

mod common;

use std::future::pending;
use std::time::Duration;

use common::handler;
use linch_rpc::{Client, ErrorObject, code};
use serde_json::{Value, json};
use tokio::io::{
    AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream, Lines, ReadHalf, WriteHalf,
};
use tokio::sync::mpsc;
use tokio::time::timeout;
use tokio_util::sync::CancellationToken;

/// The server's end of a connection to a [`Client`].
struct FakeServer {
    input: Lines<BufReader<ReadHalf<DuplexStream>>>,
    output: WriteHalf<DuplexStream>,
}

impl FakeServer {
    fn new() -> (Self, DuplexStream) {
        let (ours, theirs) = tokio::io::duplex(64 * 1024);
        let (input, output) = tokio::io::split(ours);
        let server = Self {
            input: BufReader::new(input).lines(),
            output,
        };
        (server, theirs)
    }

    async fn send(&mut self, message: Value) {
        let line = format!("{message}\n");
        self.output.write_all(line.as_bytes()).await.unwrap();
    }

    async fn recv(&mut self) -> Value {
        let line = timeout(Duration::from_secs(5), self.input.next_line());
        let line = line.await.expect("client silent").unwrap().expect("eof");
        serde_json::from_str(&line).unwrap()
    }
}

fn request(id: &str, method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

#[tokio::test]
async fn the_client_answers_server_requests_and_drops_cancelled_ones() {
    let (tokens, mut arrived) = mpsc::unbounded_channel::<CancellationToken>();
    let answer = handler(move |method, params, cancel| {
        let tokens = tokens.clone();
        async move {
            match method.as_str() {
                "echo" => Ok(Value::Object(params)),
                "hang" => {
                    tokens.send(cancel).unwrap();
                    pending().await
                }
                _ => Err(ErrorObject::new(7, "failed")),
            }
        }
    });
    let (mut server, theirs) = FakeServer::new();
    let (reader, writer) = tokio::io::split(theirs);
    let (_client, mut notifications) = Client::connect_with(reader, writer, answer);

    server.send(request("srv-1", "echo", json!({"x": 1}))).await;
    assert_eq!(
        server.recv().await,
        json!({"jsonrpc": "2.0", "id": "srv-1", "result": {"x": 1}})
    );

    server.send(request("srv-2", "hang", json!({}))).await;
    let token = arrived.recv().await.unwrap();
    let cancel = json!({"jsonrpc": "2.0", "method": "$/cancelRequest",
                        "params": {"id": "srv-2"}});
    server.send(cancel).await;
    timeout(Duration::from_secs(5), token.cancelled())
        .await
        .expect("cancelled");

    // The next answer is srv-3's: srv-2 got none.
    server.send(request("srv-3", "other", json!({}))).await;
    let failed = server.recv().await;
    assert_eq!(failed["id"], "srv-3");
    assert_eq!(failed["error"]["code"], 7);

    // `$/cancelRequest` is the client's business; other notifications pass.
    let end = json!({"jsonrpc": "2.0", "method": "run.end", "params": {"run_id": "r"}});
    server.send(end).await;
    let notification = notifications.recv().await.unwrap();
    assert_eq!(notification.method, "run.end");
}

#[tokio::test]
async fn a_client_without_a_handler_refuses_server_requests() {
    let (mut server, theirs) = FakeServer::new();
    let (reader, writer) = tokio::io::split(theirs);
    let (_client, _notifications) = Client::connect(reader, writer);
    server.send(request("srv-1", "tool.call", json!({}))).await;
    let refused = server.recv().await;
    assert_eq!(refused["id"], "srv-1");
    assert_eq!(refused["error"]["code"], code::METHOD_NOT_FOUND);
}

#[tokio::test]
async fn a_closed_connection_cancels_requests_being_answered() {
    let (tokens, mut arrived) = mpsc::unbounded_channel::<CancellationToken>();
    let answer = handler(move |_, _, cancel| {
        tokens.send(cancel).unwrap();
        pending::<Result<Value, ErrorObject>>()
    });
    let (mut server, theirs) = FakeServer::new();
    let (reader, writer) = tokio::io::split(theirs);
    let (client, _notifications) = Client::connect_with(reader, writer, answer);
    server.send(request("srv-1", "hang", json!({}))).await;
    let token = arrived.recv().await.unwrap();
    let call = tokio::spawn(async move {
        client
            .call::<_, Value>("run.cancel", &json!({"run_id": "r"}))
            .await
    });
    server.recv().await;

    drop(server);
    timeout(Duration::from_secs(5), token.cancelled())
        .await
        .expect("cancelled at close");
    let failed = timeout(Duration::from_secs(5), call)
        .await
        .unwrap()
        .unwrap();
    assert!(
        matches!(failed, Err(linch_rpc::ClientError::Closed)),
        "{failed:?}"
    );
}
