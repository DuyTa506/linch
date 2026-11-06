//! Every write pins READ COMMITTED. Under a server or role default of
//! REPEATABLE READ, a write that waited on a row lock would otherwise fail
//! with a serialization error once the holder commits, instead of reading
//! the row again.

use std::future::Future;
use std::time::{Duration, Instant};

use linch_core::{Commit, LogEntry, Message, MessageEntry, RunId, RunStore, SessionId};
use linch_core::{SessionStore, StoreError};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use sqlx::AssertSqlSafe;
use sqlx::postgres::PgPoolOptions;

use crate::common::{TestDb, envelope, new_run};

/// A pool on the test schema whose transactions default to REPEATABLE READ.
async fn repeatable_read(db: &TestDb) -> PgPool {
    let options =
        (db.options.clone()).options([("default_transaction_isolation", "repeatable read")]);
    PgPoolOptions::new()
        .max_connections(2)
        .connect_with(options)
        .await
        .expect("connect with repeatable read")
}

/// Run `write` while another transaction holds an update of the rows
/// `update` touches, and commit that transaction once `write` waits on it.
async fn behind_an_update<T>(db: &TestDb, update: &str, write: impl Future<Output = T>) -> T {
    let mut holder = db.admin.begin().await.unwrap();
    let sql = format!("SET LOCAL search_path TO {}", db.schema);
    sqlx::query(AssertSqlSafe(sql))
        .execute(&mut *holder)
        .await
        .unwrap();
    let query = sqlx::query(AssertSqlSafe(update.to_owned()));
    let updated = query.execute(&mut *holder).await.unwrap();
    assert_eq!(updated.rows_affected(), 1, "{update}");
    let release = async {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            // pg_locks, unlike pg_stat_activity, is not a snapshot per
            // transaction.
            let blocked: Option<i32> = sqlx::query_scalar(
                "SELECT pid FROM pg_locks \
                 WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))",
            )
            .fetch_optional(&mut *holder)
            .await
            .unwrap();
            if blocked.is_some() {
                break;
            }
            assert!(Instant::now() < deadline, "the write never waited");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        holder.commit().await.unwrap();
    };
    let (result, ()) = tokio::join!(write, release);
    result
}

#[tokio::test]
async fn a_claim_behind_an_update_reads_the_row_again() {
    let Some(db) = TestDb::migrated("rr_claim").await else {
        return;
    };
    let store = PgRunStore::new(repeatable_read(&db).await);
    store.create(new_run("ns", "r1", 0)).await.unwrap();
    let id = RunId::new("r1");
    let update = "UPDATE linch_runs SET owner = 'other' WHERE run_id = 'r1'";
    let lease = behind_an_update(&db, update, store.claim("ns", &id, "a")).await;
    assert_eq!(lease.expect("the claim").epoch, 1);
    db.finish().await;
}

#[tokio::test]
async fn a_commit_behind_an_update_reads_the_row_again() {
    let Some(db) = TestDb::migrated("rr_commit").await else {
        return;
    };
    let store = PgRunStore::new(repeatable_read(&db).await);
    store.create(new_run("ns", "r1", 0)).await.unwrap();
    let id = RunId::new("r1");
    let lease = store.claim("ns", &id, "a").await.unwrap();
    let commit = Commit {
        events: vec![envelope(&id, 1)],
        ..Commit::default()
    };
    let update = "UPDATE linch_runs SET owner = 'other' WHERE run_id = 'r1'";
    let result = behind_an_update(&db, update, store.commit(&lease, commit)).await;
    result.expect("the commit");
    assert_eq!(store.load("ns", &id).await.unwrap().last_seq, 1);
    db.finish().await;
}

#[tokio::test]
async fn an_append_behind_an_update_reads_the_row_again() {
    let Some(db) = TestDb::migrated("rr_append").await else {
        return;
    };
    let store = PgSessionStore::new(repeatable_read(&db).await);
    let id = SessionId::new("s1");
    store.create("ns", &id).await.unwrap();
    let entry = LogEntry::Message(MessageEntry {
        message: Message::user_text("hi").into(),
        visible: true,
        historical: true,
    });
    let update = "UPDATE linch_sessions SET len = len WHERE session_id = 's1'";
    let append = store.append("ns", &id, 0, vec![entry]);
    let result: Result<u64, StoreError> = behind_an_update(&db, update, append).await;
    assert_eq!(result.expect("the append"), 1);
    db.finish().await;
}
