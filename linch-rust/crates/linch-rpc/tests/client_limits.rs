//! The client's own limits, against a fake server: once its reader stops,
//! calls fail at once and the output is shut down; notifications back up
//! instead of piling up; an oversized answer fails only its call.

use std::time::Duration;

use linch_rpc::{Client, ClientError, MAX_LINE_BYTES, NOTIFICATION_CAPACITY, code};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::time::timeout;

async fn cancel(client: &Client) -> Result<Value, ClientError> {
    let params = json!({"run_id": "r"});
    let call = client.call::<_, Value>("run.cancel", &params);
    timeout(Duration::from_secs(5), call)
        .await
        .expect("no hang")
}

#[tokio::test]
async fn after_the_reader_stops_calls_fail_at_once_and_the_output_ends() {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (reader, writer) = tokio::io::split(theirs);
    let (client, mut notifications) = Client::connect(reader, writer);
    let (server_input, mut server_output) = tokio::io::split(ours);
    server_output.write_all(b"not a message\n").await.unwrap();
    assert!(notifications.recv().await.is_none());

    assert!(matches!(cancel(&client).await, Err(ClientError::Closed)));
    let mut lines = BufReader::new(server_input).lines();
    let end = timeout(Duration::from_secs(5), lines.next_line()).await;
    assert_eq!(end.expect("the output ends").unwrap(), None);
}

#[tokio::test]
async fn unreceived_notifications_stop_the_client_reading() {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (reader, writer) = tokio::io::split(theirs);
    let (_client, mut notifications) = Client::connect(reader, writer);
    let (_server_input, mut server_output) = tokio::io::split(ours);
    let line = format!(
        "{}\n",
        json!({"jsonrpc": "2.0", "method": "run.event", "params": {}})
    );
    let flood = async {
        for _ in 0..50 * NOTIFICATION_CAPACITY {
            server_output.write_all(line.as_bytes()).await.unwrap();
        }
    };
    let blocked = timeout(Duration::from_millis(500), flood).await;
    assert!(blocked.is_err(), "the client read everything unasked");
    for _ in 0..NOTIFICATION_CAPACITY {
        notifications.recv().await.expect("queued");
    }
}

#[tokio::test]
async fn an_oversized_answer_fails_only_its_call() {
    let (ours, theirs) = tokio::io::duplex(64 * 1024);
    let (reader, writer) = tokio::io::split(theirs);
    let (client, _notifications) = Client::connect(reader, writer);
    let (server_input, mut server_output) = tokio::io::split(ours);
    let mut requests = BufReader::new(server_input).lines();
    let serving = tokio::spawn(async move {
        let first: Value =
            serde_json::from_str(&requests.next_line().await.unwrap().unwrap()).unwrap();
        let huge = "x".repeat(MAX_LINE_BYTES);
        let answer = json!({"jsonrpc": "2.0", "id": first["id"], "result": {"pad": huge}});
        server_output
            .write_all(format!("{answer}\n").as_bytes())
            .await
            .unwrap();
        let second: Value =
            serde_json::from_str(&requests.next_line().await.unwrap().unwrap()).unwrap();
        let answer = json!({"jsonrpc": "2.0", "id": second["id"], "result": {"ok": true}});
        server_output
            .write_all(format!("{answer}\n").as_bytes())
            .await
            .unwrap();
        (server_output, requests)
    });
    match cancel(&client).await {
        Err(ClientError::Rpc(error)) => assert_eq!(error.code, code::INVALID_REQUEST),
        other => panic!("expected the oversized answer to fail, got {other:?}"),
    }
    assert_eq!(cancel(&client).await.unwrap(), json!({"ok": true}));
    drop(serving.await.unwrap());
}
