//! An open whose migrations never finish (a held migration lock, a stalled
//! server) gives up by itself instead of waiting forever. The server here
//! is a fake that logs the client in, answers pings, and never answers a
//! query; no database is needed.

use std::net::SocketAddr;
use std::time::{Duration, Instant};

use linch::{AgentConfig, OpenError};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

const READY: [u8; 6] = [b'Z', 0, 0, 0, 5, b'I'];

/// A Postgres server that completes every login and then answers only
/// pings (a lone `Sync`), so the first query waits forever.
async fn stalls_after_login() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(serve(stream));
        }
    });
    address
}

async fn serve(mut stream: TcpStream) -> std::io::Result<()> {
    let length = stream.read_u32().await? as usize;
    let mut startup = vec![0; length - 4];
    stream.read_exact(&mut startup).await?;
    stream.write_all(&[b'R', 0, 0, 0, 8, 0, 0, 0, 0]).await?;
    stream.write_all(&READY).await?;
    let mut querying = false;
    loop {
        let kind = stream.read_u8().await?;
        let length = stream.read_u32().await? as usize;
        let mut body = vec![0; length - 4];
        stream.read_exact(&mut body).await?;
        match kind {
            b'X' => return Ok(()),
            b'S' if !querying => stream.write_all(&READY).await?,
            _ => querying = true,
        }
    }
}

#[tokio::test]
async fn an_open_whose_migrations_never_finish_gives_up() {
    let address = stalls_after_login().await;
    let config: AgentConfig = serde_json::from_value(json!({
        "namespace": "t",
        "spec": {"model": "scripted-model"},
        "provider": {"id": "scripted", "turns": []},
        "stores": {"type": "postgres",
                   "dsn": format!("postgres://linch@{address}/linch?sslmode=disable")},
    }))
    .unwrap();
    let started = Instant::now();
    let opened = tokio::time::timeout(Duration::from_secs(90), config.open()).await;
    let error = opened.expect("the open gives up by itself").unwrap_err();
    assert!(matches!(error, OpenError::Store(_)), "{error:?}");
    assert!(error.to_string().contains("migrations"), "{error}");
    // It got past the connect, which gives up after 5 s.
    assert!(started.elapsed() > Duration::from_secs(10));
}
