//! A connection whose client stops reading ends once its output has made no
//! progress for the write stall timeout after the connection stops, and
//! frees its tenant place; a client that reads slowly but steadily is not
//! cut. Time is paused: the sleeps below are virtual.

use std::sync::Arc;
use std::time::Duration;

use linch_rpc::{Access, Allowlists, Tenant, serve_lines};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream, WriteHalf};
use tokio_util::sync::CancellationToken;

/// A tenant with one connection place, taken by a connection over a small
/// transport; the client has sent `requests` and keeps its input open.
struct Stalled {
    tenant: Arc<Tenant>,
    stop: CancellationToken,
    serving: tokio::task::JoinHandle<std::io::Result<()>>,
    reader: tokio::io::ReadHalf<DuplexStream>,
    writer: WriteHalf<DuplexStream>,
}

async fn stalled(requests: u64) -> Stalled {
    let tenant = Arc::new(Tenant::new("acme", Allowlists::new().max_connections(1)).unwrap());
    let place = Arc::clone(&tenant).connect().unwrap();
    let (ours, theirs) = tokio::io::duplex(256);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let stop = CancellationToken::new();
    let serving = tokio::spawn(serve_lines(
        server_reader,
        server_writer,
        Access::Tenant(place),
        stop.clone(),
    ));
    let (reader, mut writer) = tokio::io::split(ours);
    for id in 0..requests {
        let line = format!("{{\"jsonrpc\":\"2.0\",\"id\":{id},\"method\":\"server.info\"}}\n");
        writer.write_all(line.as_bytes()).await.unwrap();
    }
    // The answers fill the transport; the writer waits for the client.
    tokio::time::sleep(Duration::from_millis(200)).await;
    Stalled {
        tenant,
        stop,
        serving,
        reader,
        writer,
    }
}

fn place_is_free(tenant: &Arc<Tenant>) -> bool {
    Arc::clone(tenant).connect().is_ok()
}

#[tokio::test(start_paused = true)]
async fn stop_ends_a_connection_whose_client_does_not_read() {
    let client = stalled(32).await;
    client.stop.cancel();
    let ended = tokio::time::timeout(Duration::from_secs(10), client.serving).await;
    assert!(
        ended.is_ok(),
        "10 s after stop the connection is still open"
    );
    assert!(place_is_free(&client.tenant), "its tenant place is held");
}

#[tokio::test(start_paused = true)]
async fn the_end_of_input_ends_a_connection_whose_client_does_not_read() {
    let mut client = stalled(32).await;
    client.writer.shutdown().await.unwrap();
    let ended = tokio::time::timeout(Duration::from_secs(10), client.serving).await;
    assert!(
        ended.is_ok(),
        "10 s after the input ended the connection is still open"
    );
    assert!(place_is_free(&client.tenant), "its tenant place is held");
}

/// A client that reads one answer every 2 s keeps the writer moving, so
/// stopping the server does not cut it: every answer arrives.
#[tokio::test(start_paused = true)]
async fn a_client_that_reads_slowly_but_steadily_is_not_cut_after_stop() {
    let client = stalled(32).await;
    client.stop.cancel();
    let mut lines = BufReader::new(client.reader).lines();
    let mut seen = 0;
    while let Ok(Some(_)) = lines.next_line().await {
        seen += 1;
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    assert_eq!(seen, 32, "answers read before the connection closed");
    let ended = client.serving.await.unwrap();
    assert!(ended.is_ok(), "{ended:?}");
    assert!(place_is_free(&client.tenant));
}
