//! linch-core's store conformance suites, plus exact round trips.

use linch_core::store::conformance::{run_admission_suite, run_store_suite, session_store_suite};
use linch_core::{JsonValue, RunId, RunStore};
use linch_postgres::{PgRunStore, PgSessionStore};

use crate::common::{TestDb, envelope, new_run};

#[tokio::test]
async fn run_store_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("conformance_run").await else {
        return;
    };
    run_store_suite(&PgRunStore::new(db.pool.clone())).await;
    db.finish().await;
}

#[tokio::test]
async fn run_store_passes_the_admission_suite() {
    let Some(db) = TestDb::migrated("conformance_admission").await else {
        return;
    };
    run_admission_suite(&PgRunStore::new(db.pool.clone())).await;
    db.finish().await;
}

#[tokio::test]
async fn session_store_passes_the_conformance_suite() {
    let Some(db) = TestDb::migrated("conformance_session").await else {
        return;
    };
    session_store_suite(&PgSessionStore::new(db.pool.clone())).await;
    db.finish().await;
}

/// Stored JSON comes back byte for byte: key order and number text survive.
#[tokio::test]
async fn events_round_trip_exactly() {
    let Some(db) = TestDb::migrated("round_trip").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let mut run = new_run("ns", "r1", 1);
    let mut event = envelope(&run.run_id, 1);
    let linch_core::Event::Error(error) = &mut event.event else {
        unreachable!("the builder makes an error event");
    };
    let payload = r#"{"zeta":0.1,"alpha":1e+300,"mid":[3,{"b":1,"a":2}]}"#;
    let value: JsonValue = serde_json::from_str(payload).unwrap();
    error.error.insert("payload".into(), value);
    run.events = vec![event.clone()];
    store.create(run).await.unwrap();

    let raw: String = sqlx::query_scalar("SELECT envelope FROM linch_run_events")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert!(raw.contains(payload), "key order and numbers kept: {raw}");
    let id = RunId::new("r1");
    let stored = store.events("ns", &id, 0).await.unwrap();
    assert_eq!(stored, [event.clone()]);
    let text = |e: &linch_core::Envelope| e.to_value().unwrap().to_string();
    assert_eq!(text(&stored[0]), text(&event), "same bytes");
    let record = store.load("ns", &id).await.unwrap();
    assert_eq!(record.parent_run_id, None, "a run without a parent");
    db.finish().await;
}
