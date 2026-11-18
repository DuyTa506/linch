//! `run.start` answers only once the run is stored: the run id a host holds
//! exists after any crash that follows. Postgres, with the run's insert
//! slowed in the database so an answer sent before the commit shows.
//! Skipped without `LINCH_TEST_PG_DSN`.

mod common;

use std::time::Duration;

use common::pg::TestDb;
use common::*;
use linch_rpc::protocol::{RunAccepted, method};
use serde_json::{Value, json};

fn on_postgres(db: &TestDb) -> Value {
    let mut config = scripted(vec![text_turn("done")], None, "");
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

fn start(agent: &str, session: &str, run_id: &str) -> Value {
    json!({"agent": agent, "session_id": session, "prompt": "go", "run_id": run_id})
}

#[tokio::test]
async fn the_answer_waits_for_the_run_to_be_stored() {
    let Some(db) = TestDb::empty("durable_wait").await else {
        return;
    };
    let mut rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db)).await;
    db.delay_inserts("linch_runs", "0.5").await;
    let accepted: RunAccepted = {
        let call = rpc.call(method::RUN_START, start(&agent, "s1", "held"));
        tokio::pin!(call);
        let early = tokio::time::timeout(Duration::from_millis(200), &mut call).await;
        assert!(early.is_err(), "answered before the run was stored");
        serde_json::from_value(call.await.unwrap()).unwrap()
    };
    assert_eq!(accepted.run_id.as_str(), "held");
    assert!(
        db.has_run("linch_runs", "held").await,
        "answered, not stored"
    );
    let ended = rpc.until_end(&accepted.run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    db.drop().await;
}

/// An answered run id is stored the moment the host reads it, whatever the
/// commit's latency: nothing a crash after the answer can undo.
#[tokio::test]
async fn every_answered_run_id_is_stored_at_once() {
    let Some(db) = TestDb::empty("durable_every").await else {
        return;
    };
    let mut rpc = Rpc::new();
    let agent = rpc.open(on_postgres(&db)).await;
    db.delay_inserts("linch_runs", "random() * 0.15").await;
    let mut ids = Vec::new();
    for n in 0..8 {
        let id = format!("run-{n}");
        let call = rpc.call(method::RUN_START, start(&agent, &format!("s{n}"), &id));
        let accepted = call.await.unwrap();
        assert_eq!(accepted["run_id"], id);
        assert!(
            db.has_run("linch_runs", &id).await,
            "{id} answered, not stored"
        );
        ids.push(id);
    }
    for id in ids {
        let ended = rpc.until_end(&linch::RunId::new(id)).await;
        assert!(ended.error.is_none(), "{:?}", ended.error);
    }
    db.drop().await;
}
