//! A client that stops reading must not cost its run its lease. The server
//! stops polling the run when the outbox is full, but the run's heartbeat is a
//! task of its own, so the connection keeps the run for as long as it lives
//! and the run goes on once the client reads again. Skipped without
//! `LINCH_TEST_PG_DSN`.

mod common;

use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch::PgPools;
use linch::provider::scripted::TurnBuilder;
use linch_rpc::protocol::method;
use linch_rpc::{Access, code, serve_lines};
use serde_json::{Value, json};
use std::sync::Arc;
use tokio::io::{
    AsyncBufReadExt, AsyncWriteExt, BufReader, DuplexStream, Lines, ReadHalf, WriteHalf, split,
};
use tokio_util::sync::CancellationToken;

/// The lease the agent holds, in milliseconds (the shortest the config takes).
const LEASE_MS: u64 = 1000;

/// Text deltas of one model call: far more than the outbox (1024 messages)
/// and the transport can hold, so the run blocks on the client.
const DELTAS: usize = 5000;

fn on_postgres(db: &TestDb, turns: Vec<linch::ScriptedTurn>) -> Value {
    let mut config = scripted(turns, None, "");
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn, "lease_ttl_ms": LEASE_MS});
    config
}

/// One connection's raw lines.
struct Raw {
    writer: WriteHalf<DuplexStream>,
    lines: Lines<BufReader<ReadHalf<DuplexStream>>>,
}

impl Raw {
    /// A connection over a transport that holds 1 KiB: a client that does
    /// not read stops the server's writer after one line.
    fn connect() -> Self {
        let (ours, theirs) = tokio::io::duplex(1024);
        let (reader, writer) = split(theirs);
        let access = Access::Local(Arc::new(PgPools::new()));
        tokio::spawn(serve_lines(
            reader,
            writer,
            access,
            CancellationToken::new(),
        ));
        let (reader, writer) = split(ours);
        Self {
            writer,
            lines: BufReader::new(reader).lines(),
        }
    }

    async fn next(&mut self) -> Value {
        let line = tokio::time::timeout(Duration::from_secs(20), self.lines.next_line());
        let line = line
            .await
            .expect("a line in time")
            .unwrap()
            .expect("a line");
        serde_json::from_str(&line).unwrap()
    }

    /// Send a request and read until its response, skipping notifications.
    async fn call(&mut self, id: u64, method: &str, params: Value) -> Value {
        let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        self.writer
            .write_all(format!("{request}\n").as_bytes())
            .await
            .unwrap();
        loop {
            let message = self.next().await;
            if message["id"] == json!(id) {
                return message["result"].clone();
            }
        }
    }

    /// Read to the run's `run.end`; its error, and the kind of the last
    /// envelope that came before.
    async fn until_end(&mut self) -> (Value, Option<String>) {
        let mut last = None;
        loop {
            let message = self.next().await;
            match message["method"].as_str() {
                Some(method::RUN_EVENT) => {
                    let envelopes = message["params"]["envelopes"].as_array().unwrap();
                    last = envelopes.last().map(|e| e["event"]["type"].to_string());
                }
                Some(method::RUN_END) => return (message["params"]["error"].clone(), last),
                _ => {}
            }
        }
    }
}

/// The client reads the run's first lines, then stops for two leases while
/// the run waits on a full outbox. The run stays its server's: a resume of
/// it through another server is refused as leased. When the client reads
/// again, the run goes on to its result.
#[tokio::test]
async fn a_client_that_stops_reading_keeps_its_run() {
    let Some(db) = TestDb::empty("stalled_reader").await else {
        return;
    };
    let mut streaming = TurnBuilder::new(MODEL);
    for _ in 0..DELTAS {
        streaming = streaming.text("x".repeat(100));
    }
    let mut a = Raw::connect();
    let opened = a
        .call(
            1,
            method::AGENT_OPEN,
            on_postgres(&db, vec![streaming.build()]),
        )
        .await;
    let agent = opened["agent"].as_str().unwrap().to_owned();
    let params = json!({"agent": agent, "session_id": "s1", "prompt": "go"});
    let started = a.call(2, method::RUN_START, params).await;
    let run_id = started["run_id"].as_str().unwrap().to_owned();

    // The client reads nothing now.
    tokio::time::sleep(Duration::from_millis(LEASE_MS * 5 / 2)).await;

    let mut b = Rpc::new();
    let other = b.open(on_postgres(&db, vec![text_turn("from b")])).await;
    let params = json!({"agent": other, "run_id": run_id});
    b.accepted(method::RUN_RESUME, params).await;
    let refused = b.until_end(&run_id.as_str().into()).await;
    let error = refused.error.expect("the run is leased to its server");
    assert_eq!(error.code, code::RUN_LEASED, "{error:?}");

    let (error, last) = a.until_end().await;
    assert!(error.is_null(), "the run ended with {error}");
    assert_eq!(last.as_deref(), Some("\"result\""));
    db.drop().await;
}
