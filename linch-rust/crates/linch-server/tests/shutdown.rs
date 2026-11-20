//! The binary on SIGTERM with a client that stays connected and never reads:
//! it exits within the shutdown grace (15 s) plus a margin, whether the
//! client left a run streaming or thousands of answers queued.

mod common;

use std::process::Stdio;
use std::time::{Duration, Instant};

use common::TOKEN;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::net::unix::OwnedReadHalf;
use tokio::process::{Child, Command};
use tokio::time::{sleep, timeout};

/// The documented grace after a stop.
const GRACE: Duration = Duration::from_secs(15);

struct Running {
    child: Child,
    socket: std::path::PathBuf,
    _dir: tempfile::TempDir,
}

async fn start() -> Running {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("linch.sock");
    let config = json!({"socket": socket, "tenants": {"acme": {
        "tokens": [{"env": "LINCH_SHUTDOWN_TOKEN"}], "allow": {"scripted_provider": true}}}});
    let file = dir.path().join("server.json");
    std::fs::write(&file, config.to_string()).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_linch-server"))
        .arg("--config")
        .arg(&file)
        .env("LINCH_SHUTDOWN_TOKEN", TOKEN)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    for _ in 0..1000 {
        if socket.exists() {
            return Running {
                child,
                socket,
                _dir: dir,
            };
        }
        sleep(Duration::from_millis(20)).await;
    }
    panic!("the socket never appeared");
}

/// Log in and return the reading half (so far read up to the login answer)
/// and the writing half.
async fn login(
    socket: &std::path::Path,
) -> (BufReader<OwnedReadHalf>, tokio::net::unix::OwnedWriteHalf) {
    let (reader, mut writer) = UnixStream::connect(socket).await.unwrap().into_split();
    let auth = json!({"jsonrpc": "2.0", "id": 0, "method": "connection.auth",
                      "params": {"token": TOKEN}});
    writer
        .write_all(format!("{auth}\n").as_bytes())
        .await
        .unwrap();
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    timeout(Duration::from_secs(20), reader.read_line(&mut line))
        .await
        .expect("a login answer")
        .unwrap();
    let answer: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(answer["result"]["tenant"], "acme", "{answer}");
    (reader, writer)
}

/// SIGTERM by pid, then how long the process takes to exit.
async fn sigterm_and_wait(running: &mut Running) -> Duration {
    let pid = running.child.id().unwrap();
    let sent = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .await
        .unwrap();
    assert!(sent.success());
    let started = Instant::now();
    let waited = timeout(GRACE + Duration::from_secs(10), running.child.wait()).await;
    let status = waited.expect("the server exits on SIGTERM").unwrap();
    assert!(status.success(), "{status}");
    started.elapsed()
}

#[tokio::test]
async fn a_server_with_a_run_streaming_to_a_client_that_never_reads_exits_within_the_grace() {
    let mut running = start().await;
    let (mut reader, mut writer) = login(&running.socket).await;
    let turn = linch::provider::scripted::TurnBuilder::new("scripted-model")
        .text("x".repeat(12 * 1024 * 1024))
        .build();
    let open = json!({"jsonrpc": "2.0", "id": 1, "method": "agent.open", "params": {
        "namespace": "acme", "spec": {"model": "scripted-model", "max_turns": 100},
        "provider": {"id": "scripted", "turns": [turn]}}});
    writer
        .write_all(format!("{open}\n").as_bytes())
        .await
        .unwrap();
    let mut line = String::new();
    timeout(Duration::from_secs(60), reader.read_line(&mut line))
        .await
        .expect("an open answer")
        .unwrap();
    let opened: Value = serde_json::from_str(&line).unwrap();
    let agent = opened["result"]["agent"].clone();
    let start = json!({"jsonrpc": "2.0", "id": 2, "method": "run.start",
                       "params": {"agent": agent, "session_id": "s1", "prompt": "hi"}});
    writer
        .write_all(format!("{start}\n").as_bytes())
        .await
        .unwrap();
    // Connected and never reading again: the run fills the socket.
    sleep(Duration::from_secs(2)).await;
    let took = sigterm_and_wait(&mut running).await;
    assert!(
        took <= GRACE + Duration::from_secs(2),
        "exited {took:?} after SIGTERM"
    );
    drop((reader, writer));
}

#[tokio::test]
async fn a_server_with_thousands_of_unread_answers_exits_within_the_grace() {
    let mut running = start().await;
    let (reader, mut writer) = login(&running.socket).await;
    // The server stops reading once its queue is full, so the writes come
    // from a task of their own.
    let writing = tokio::spawn(async move {
        for id in 1..=5000 {
            let request = json!({"jsonrpc": "2.0", "id": id, "method": "server.info"});
            if writer
                .write_all(format!("{request}\n").as_bytes())
                .await
                .is_err()
            {
                return writer;
            }
        }
        writer
    });
    sleep(Duration::from_secs(2)).await;
    let took = sigterm_and_wait(&mut running).await;
    assert!(
        took <= GRACE + Duration::from_secs(2),
        "exited {took:?} after SIGTERM"
    );
    drop((reader, writing));
}
