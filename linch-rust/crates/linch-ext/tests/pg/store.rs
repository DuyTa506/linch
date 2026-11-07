//! `PgJournalStore` on the conformance suite, a claim racing an append (at
//! the server's default isolation and under REPEATABLE READ), exact round
//! trips and the migrations.

use std::time::Duration;

use linch_ext::workflow::conformance::journal_store_suite;
use linch_ext::workflow::{
    JournalRecord, JournalStore, JournalStoreError, PgJournalStore, WorkflowStatus,
};
use serde_json::json;

use crate::common::TestDb;

/// A claim that commits while an append is in flight fences the append:
/// the append waits for the claim's row lock, then sees the new epoch and
/// writes nothing, so the new owner never misses a record it loaded past.
#[tokio::test]
async fn an_append_racing_a_claim_never_lands_under_the_old_epoch() {
    let Some(mut db) = TestDb::migrated("race").await else {
        return;
    };
    let pool = db.pool().await;
    let store = PgJournalStore::new(pool.clone());
    let lease = store.claim("ns", "w1").await.unwrap();
    // The claim of another process, still open.
    let mut claim = pool.begin().await.unwrap();
    sqlx::query("UPDATE linch_ext_workflows SET epoch = epoch + 1 WHERE workflow_id = 'w1'")
        .execute(&mut *claim)
        .await
        .unwrap();
    let record = JournalRecord {
        call_key: "k".into(),
        occurrence: 0,
        value: json!(1),
    };
    let (appended, committed) = tokio::join!(store.append(&lease, record), async {
        tokio::time::sleep(Duration::from_millis(200)).await;
        claim.commit().await
    });
    committed.unwrap();
    assert!(
        matches!(appended, Err(JournalStoreError::LeaseLost { epoch: 1, .. })),
        "{appended:?}"
    );
    let run = store.load("ns", "w1").await.unwrap().unwrap();
    assert!(run.records.is_empty());
    db.finish().await;
}

/// Another process's claim, not committed yet.
const OPEN_CLAIM: &str =
    "UPDATE linch_ext_workflows SET epoch = epoch + 1 WHERE workflow_id = 'w1'";

fn record(call_key: &str) -> JournalRecord {
    JournalRecord {
        call_key: call_key.into(),
        occurrence: 0,
        value: json!(1),
    }
}

/// Under a REPEATABLE READ default, a write that waited on a claim gets a
/// serialization error once the claim commits; it is run again and then
/// sees the new epoch.
#[tokio::test]
async fn under_repeatable_read_an_append_behind_a_claim_is_fenced() {
    let Some(mut db) = TestDb::migrated("rr_append").await else {
        return;
    };
    let store = PgJournalStore::new(db.repeatable_read().await);
    let lease = store.claim("ns", "w1").await.unwrap();
    let appended = db.behind_an_update(OPEN_CLAIM, store.append(&lease, record("k")));
    let appended = appended.await;
    assert!(
        matches!(appended, Err(JournalStoreError::LeaseLost { epoch: 1, .. })),
        "{appended:?}"
    );
    let run = store.load("ns", "w1").await.unwrap().unwrap();
    assert!(run.records.is_empty());
    db.finish().await;
}

#[tokio::test]
async fn under_repeatable_read_a_status_write_behind_a_claim_is_fenced() {
    let Some(mut db) = TestDb::migrated("rr_status").await else {
        return;
    };
    let store = PgJournalStore::new(db.repeatable_read().await);
    let lease = store.claim("ns", "w1").await.unwrap();
    let status = store.set_status(&lease, WorkflowStatus::Completed);
    let status = db.behind_an_update(OPEN_CLAIM, status).await;
    assert!(
        matches!(status, Err(JournalStoreError::LeaseLost { epoch: 1, .. })),
        "{status:?}"
    );
    let run = store.load("ns", "w1").await.unwrap().unwrap();
    assert_eq!((run.status, run.epoch), (WorkflowStatus::Running, 2));
    db.finish().await;
}

/// A claim always wins, also behind another claim under REPEATABLE READ.
#[tokio::test]
async fn under_repeatable_read_a_claim_behind_a_claim_wins() {
    let Some(mut db) = TestDb::migrated("rr_claim").await else {
        return;
    };
    let store = PgJournalStore::new(db.repeatable_read().await);
    store.claim("ns", "w1").await.unwrap();
    let lease = db
        .behind_an_update(OPEN_CLAIM, store.claim("ns", "w1"))
        .await;
    assert_eq!(lease.expect("the claim").epoch, 3);
    db.finish().await;
}

#[tokio::test]
async fn the_postgres_store_passes_the_conformance_suite() {
    let Some(mut db) = TestDb::migrated("conformance").await else {
        return;
    };
    journal_store_suite(&PgJournalStore::new(db.pool().await)).await;
    db.finish().await;
}

/// A value reads back byte for byte: key order and number text survive.
#[tokio::test]
async fn a_record_value_round_trips_exactly() {
    let Some(mut db) = TestDb::migrated("round_trip").await else {
        return;
    };
    let store = PgJournalStore::new(db.pool().await);
    let text = r#"{"zeta":0.1,"alpha":1e+300,"mid":[3,{"b":1,"a":2}],"pi":3.141592653589793}"#;
    let lease = store.claim("ns", "w1").await.unwrap();
    let record = JournalRecord {
        call_key: "k".into(),
        occurrence: 0,
        value: serde_json::from_str(text).unwrap(),
    };
    store.append(&lease, record.clone()).await.unwrap();
    let run = store.load("ns", "w1").await.unwrap().unwrap();
    let written = serde_json::to_string(&record.value).unwrap();
    assert_eq!(run.records, [record]);
    let read = serde_json::to_string(&run.records[0].value).unwrap();
    assert_eq!(read, written, "key order and number text survive");
    db.finish().await;
}

#[tokio::test]
async fn migrate_is_idempotent_and_uses_its_own_table() {
    let Some(mut db) = TestDb::migrated("migrations").await else {
        return;
    };
    let pool = db.pool().await;
    linch_ext::migrate(&pool).await.expect("a second migrate");
    let applied: Vec<(i64, String)> =
        sqlx::query_as("SELECT version, description FROM linch_ext_migrations ORDER BY version")
            .fetch_all(&pool)
            .await
            .expect("read the migrations table");
    assert_eq!(applied, [(1, "workflow".to_owned())]);
    let sqlx_default: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('_sqlx_migrations')::text")
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(sqlx_default, None);
    assert_eq!(linch_ext::MIGRATIONS_TABLE, "linch_ext_migrations");
    db.finish().await;
}
