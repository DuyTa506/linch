//! A write whose connection dies before it ends leaves nothing behind.
//!
//! Each write touches its parent row and its child rows: a run commit and a
//! session append in one statement, the other writes as a transaction that
//! updates the parent row first. The test locks the child table, waits until
//! the write's backend blocks on that lock, terminates that backend, and
//! checks that nothing landed.

use std::future::Future;
use std::time::{Duration, Instant};

use linch_core::{Commit, LogEntry, MessageEntry, RunId, RunStore, SessionId, SessionStore};
use linch_core::{Message, StoreError};
use linch_postgres::{PgRunStore, PgSessionStore};
use sqlx::AssertSqlSafe;

use crate::common::{TestDb, checkpoint, envelope, new_run};

/// Run `write` while `table` is locked and kill its backend once it blocks.
async fn crash_on_insert<T>(db: &TestDb, table: &str, write: impl Future<Output = T>) -> T {
    let mut locker = db.admin.begin().await.unwrap();
    let lock = format!("LOCK TABLE {}.{table} IN EXCLUSIVE MODE", db.schema);
    let query = sqlx::query(AssertSqlSafe(lock));
    query.execute(&mut *locker).await.unwrap();
    let kill = async {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let blocked: Option<i32> = sqlx::query_scalar(
                "SELECT pid FROM pg_stat_activity \
                 WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid))",
            )
            .fetch_optional(&mut *locker)
            .await
            .unwrap();
            if let Some(pid) = blocked {
                let killed: bool = sqlx::query_scalar("SELECT pg_terminate_backend($1)")
                    .bind(pid)
                    .fetch_one(&mut *locker)
                    .await
                    .unwrap();
                assert!(killed, "terminate the blocked backend");
                return;
            }
            assert!(
                Instant::now() < deadline,
                "the write never blocked on {table}"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    };
    let (result, ()) = tokio::join!(write, kill);
    locker.rollback().await.unwrap();
    result
}

fn assert_backend<T: std::fmt::Debug>(result: Result<T, StoreError>) {
    assert!(matches!(result, Err(StoreError::Backend(_))), "{result:?}");
}

#[tokio::test]
async fn a_crash_inside_create_leaves_no_run() {
    let Some(db) = TestDb::migrated("crash_create").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let run = new_run("ns", "r1", 2);
    let id = run.run_id.clone();
    let result = crash_on_insert(&db, "linch_run_events", store.create(run.clone())).await;
    assert_backend(result);
    let missing = store.load("ns", &id).await;
    assert!(
        matches!(missing, Err(StoreError::NotFound { .. })),
        "{missing:?}"
    );
    store.create(run).await.expect("the same create succeeds");
    assert_eq!(store.events("ns", &id, 0).await.unwrap().len(), 2);
    db.finish().await;
}

#[tokio::test]
async fn a_crash_inside_commit_changes_nothing() {
    let Some(db) = TestDb::migrated("crash_commit").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    store.create(new_run("ns", "r1", 1)).await.unwrap();
    let id = RunId::new("r1");
    let lease = store.claim("ns", &id, "w").await.unwrap();
    let before = store.load("ns", &id).await.unwrap();
    let commit = Commit {
        events: vec![envelope(&id, 2), envelope(&id, 3)],
        checkpoint: Some(checkpoint(1)),
        status: Some(linch_core::RunStatus::AwaitingApproval),
    };
    let result = crash_on_insert(
        &db,
        "linch_run_events",
        store.commit(&lease, commit.clone()),
    );
    assert_backend(result.await);
    let after = store.load("ns", &id).await.unwrap();
    assert_eq!(
        (after.status, after.last_seq, &after.checkpoint),
        (before.status, before.last_seq, &before.checkpoint),
        "the run row update rolled back"
    );
    assert_eq!(store.events("ns", &id, 0).await.unwrap().len(), 1);
    store
        .commit(&lease, commit)
        .await
        .expect("the same commit succeeds");
    assert_eq!(store.load("ns", &id).await.unwrap().last_seq, 3);
    db.finish().await;
}

#[tokio::test]
async fn a_crash_inside_append_changes_nothing() {
    let Some(db) = TestDb::migrated("crash_append").await else {
        return;
    };
    let store = PgSessionStore::new(db.pool.clone());
    let id = SessionId::new("s1");
    store.create("ns", &id).await.unwrap();
    let entry = LogEntry::Message(MessageEntry {
        message: Message::user_text("hi").into(),
        visible: true,
        historical: true,
    });
    let entries = vec![entry.clone(), entry];
    let append = store.append("ns", &id, 0, entries.clone());
    assert_backend(crash_on_insert(&db, "linch_session_entries", append).await);
    assert!(store.load("ns", &id).await.unwrap().is_empty());
    let len = store.append("ns", &id, 0, entries).await;
    assert_eq!(
        len.expect("the same append succeeds"),
        2,
        "the length rolled back"
    );
    db.finish().await;
}
