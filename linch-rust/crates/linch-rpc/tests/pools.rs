//! Connections served with one `PgPools` share a Postgres pool per DSN,
//! and `agent.close` on one leaves the pool to the others. Skipped without
//! `LINCH_TEST_PG_DSN`.

mod common;

use std::num::NonZeroU32;
use std::sync::Arc;

use common::pg::TestDb;
use common::*;
use linch::{Event, PgPools};
use linch_rpc::protocol::method;
use linch_rpc::{Access, Allowlists, Tenant};
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

#[tokio::test]
async fn two_tenants_on_one_target_get_pools_of_their_own_sizes() {
    let Some(db) = TestDb::empty("pools_tenants").await else {
        return;
    };
    let tenant = |name: &str, cap: u32| {
        let allow = (Allowlists::new().scripted_provider(true))
            .postgres_dsns(std::slice::from_ref(&db.dsn))
            .unwrap()
            .max_pool_connections(NonZeroU32::new(cap).unwrap());
        Arc::new(Tenant::new(name, allow).unwrap())
    };
    let (acme, beta) = (tenant("acme", 3), tenant("beta", 2));
    let config = |namespace: &str, max: Option<u32>| {
        let mut config = on_postgres(&db, namespace);
        config["namespace"] = json!(namespace);
        if let Some(max) = max {
            config["stores"]["max_connections"] = json!(max);
        }
        config
    };
    // One server's pools, as every connection of a process shares them.
    let pools = Arc::new(PgPools::new());
    let connect = |tenant: &Arc<Tenant>| {
        let access = Access::Tenant(Arc::clone(tenant).connect().unwrap());
        Rpc::sharing(access, Arc::clone(&pools))
    };
    let first = connect(&acme);
    first.open(config("acme", Some(3))).await;
    // Beta asks for no size: its pool gets its own cap, below the default.
    let other = connect(&beta);
    other.open(config("beta", None)).await;
    assert_eq!(db.connections().await, 2, "a pool per tenant");
    // A second connection of acme shares acme's pool, at acme's size.
    let second = connect(&acme);
    second.open(config("acme", Some(1))).await;
    assert_eq!(db.connections().await, 2);
    let (acme, beta) = (format!("{acme:?}"), format!("{beta:?}"));
    assert!(acme.contains("PgPools { pools: 1, sizes: [3] }"), "{acme}");
    assert!(beta.contains("PgPools { pools: 1, sizes: [2] }"), "{beta}");
    assert!(!acme.contains(&db.dsn), "{acme}");
    db.drop().await;
}
