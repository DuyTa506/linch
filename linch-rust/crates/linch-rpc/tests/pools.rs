//! Connections served with one `PgPools` share a Postgres pool per DSN,
//! and `agent.close` on one leaves the pool to the others. Skipped without
//! `LINCH_TEST_PG_DSN`.

mod common;

use std::sync::Arc;

use common::pg::TestDb;
use common::*;
use linch::{Event, PgPools};
use linch_rpc::Access;
use linch_rpc::protocol::method;
use serde_json::{Value, json};

fn on_postgres(db: &TestDb, text: &str) -> Value {
    let mut config = scripted(vec![text_turn(text)], None, "");
    config["stores"] = json!({"type": "postgres", "dsn": db.dsn});
    config
}

#[tokio::test]
async fn two_connections_of_one_server_share_a_pool() {
    let Some(db) = TestDb::empty("pools_shared").await else {
        return;
    };
    let pools = Arc::new(PgPools::new());
    let first = Rpc::sharing(Access::Local, Arc::clone(&pools));
    let mut second = Rpc::sharing(Access::Local, pools);
    let closing = first.open(on_postgres(&db, "one")).await;
    let staying = second.open(on_postgres(&db, "two")).await;
    assert_eq!(db.connections().await, 1);

    let close = json!({"agent": closing});
    first.call(method::AGENT_CLOSE, close).await.unwrap();
    let run_id = second.start(&staying, "s1", "go").await;
    let ended = second.until_end(&run_id).await;
    assert!(ended.error.is_none(), "{:?}", ended.error);
    match ended.last() {
        Event::Result(result) => assert_eq!(result.final_text.as_deref(), Some("two")),
        other => panic!("expected a result, got {other:?}"),
    }
    db.drop().await;
}
