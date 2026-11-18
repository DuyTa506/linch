//! What retention needs of the stores: the end time of a run, the last
//! write of a session, and a not-found that is not "pruned" unless a
//! retention pass said so (the pass itself is `linch-worker`'s).

use linch_core::{
    Commit, LogEntry, Message, MessageEntry, RunId, RunStatus, RunStore, SessionId, SessionStore,
    StoreError,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use sqlx::AssertSqlSafe;

use crate::common::{TestDb, envelope, new_run};

async fn ended_at_is_set(db: &TestDb, run_id: &str) -> bool {
    sqlx::query_scalar("SELECT ended_at IS NOT NULL FROM linch_runs WHERE run_id = $1")
        .bind(run_id)
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

/// The commit that moves a run to a terminal status stamps its end time;
/// every other commit leaves it empty, and a terminal run is never written
/// again, so the stamp is the run's end.
#[tokio::test]
async fn the_terminal_commit_stamps_the_end_of_a_run() {
    let Some(db) = TestDb::migrated("ret_ended").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let run = new_run("ns", "r1", 0);
    let id = run.run_id.clone();
    store.create(run).await.unwrap();
    assert!(!ended_at_is_set(&db, "r1").await, "created");
    let lease = store.claim("ns", &id, "w").await.unwrap();
    assert!(!ended_at_is_set(&db, "r1").await, "claimed");
    let event = |seq| Commit {
        events: vec![envelope(&id, seq)],
        ..Commit::default()
    };
    store.commit(&lease, event(1)).await.unwrap();
    store
        .commit(
            &lease,
            Commit {
                status: Some(RunStatus::AwaitingApproval),
                ..event(2)
            },
        )
        .await
        .unwrap();
    assert!(!ended_at_is_set(&db, "r1").await, "suspended is not an end");
    let lease = store.claim("ns", &id, "w").await.unwrap();
    store
        .commit(
            &lease,
            Commit {
                status: Some(RunStatus::Completed),
                ..event(3)
            },
        )
        .await
        .unwrap();
    assert!(ended_at_is_set(&db, "r1").await, "completed");
    db.finish().await;
}

async fn recent(db: &TestDb) -> sqlx::Result<bool> {
    let query = sqlx::query_scalar::<_, bool>(
        "SELECT updated_at > now() - interval '1 minute' FROM linch_sessions WHERE session_id = 's'",
    );
    query.fetch_one(&db.pool).await
}

/// A session's clock moves with its create and every append.
#[tokio::test]
async fn an_append_moves_the_sessions_last_write() {
    let Some(db) = TestDb::migrated("ret_updated").await else {
        return;
    };
    let sessions = PgSessionStore::new(db.pool.clone());
    let id = SessionId::new("s");
    sessions.create("ns", &id).await.unwrap();
    assert!(recent(&db).await.unwrap(), "created");
    sqlx::query("UPDATE linch_sessions SET updated_at = now() - interval '9 days'")
        .execute(&db.pool)
        .await
        .unwrap();
    assert!(!recent(&db).await.unwrap());
    let entry = LogEntry::Message(MessageEntry {
        message: Message::user_text("hi").into(),
        visible: true,
        historical: true,
    });
    sessions.append("ns", &id, 0, vec![entry]).await.unwrap();
    assert!(recent(&db).await.unwrap(), "appended");
    db.finish().await;
}

/// A run that never existed, or exists in another namespace, is a plain
/// not-found; only a tombstone says "pruned".
#[tokio::test]
async fn a_missing_run_is_not_pruned_without_a_tombstone() {
    let Some(db) = TestDb::migrated("ret_missing").await else {
        return;
    };
    let store = PgRunStore::new(db.pool.clone());
    let id = RunId::new("nope");
    let load = store.load("ns", &id).await.unwrap_err();
    let events = store.events("ns", &id, 0).await.unwrap_err();
    let claim = store.claim("ns", &id, "w").await.unwrap_err();
    for error in [load, events, claim] {
        assert!(
            matches!(error, StoreError::NotFound { pruned: false, .. }),
            "{error:?}"
        );
    }
    sqlx::query("INSERT INTO linch_pruned_runs (namespace, run_id) VALUES ('ns', 'nope')")
        .execute(&db.pool)
        .await
        .unwrap();
    let pruned = store.load("ns", &id).await.unwrap_err();
    assert!(
        matches!(pruned, StoreError::NotFound { pruned: true, .. }),
        "{pruned:?}"
    );
    assert!(pruned.to_string().contains("retention"), "{pruned}");
    let other = store.load("other", &id).await.unwrap_err();
    assert!(
        matches!(other, StoreError::NotFound { pruned: false, .. }),
        "{other:?}"
    );
    db.finish().await;
}

/// The retention migration, applied over data written before it, rewrites
/// no row: a run that was terminal has no end time (a pass stamps it), the
/// others none, and a session starts its clock.
#[tokio::test]
async fn the_retention_migration_rewrites_no_run() {
    let Some(db) = TestDb::empty("ret_backfill").await else {
        return;
    };
    let before: [&str; 9] = [
        include_str!("../../migrations/0001_stores.sql"),
        include_str!("../../migrations/0002_one_live_run_per_session.sql"),
        include_str!("../../migrations/0003_files.sql"),
        include_str!("../../migrations/0004_tasks.sql"),
        include_str!("../../migrations/0005_mailbox.sql"),
        include_str!("../../migrations/0006_run_leases.sql"),
        include_str!("../../migrations/0007_run_principal.sql"),
        include_str!("../../migrations/0008_tool_cache.sql"),
        include_str!("../../migrations/0009_memories.sql"),
    ];
    for sql in before {
        sqlx::raw_sql(AssertSqlSafe(sql.to_owned()))
            .execute(&db.pool)
            .await
            .unwrap();
    }
    for (id, status) in [
        ("done", "completed"),
        ("bad", "failed"),
        ("stop", "aborted"),
        ("live", "running"),
        ("ask", "awaiting_approval"),
    ] {
        sqlx::query(
            "INSERT INTO linch_runs (namespace, run_id, session_id, contract, status, epoch, \
             checkpoint, last_seq) VALUES ('ns', $1, $1, 'c', $2, 1, '{}', 0)",
        )
        .bind(id)
        .bind(status)
        .execute(&db.pool)
        .await
        .unwrap();
    }
    sqlx::query("INSERT INTO linch_sessions (namespace, session_id, len) VALUES ('ns', 's', 0)")
        .execute(&db.pool)
        .await
        .unwrap();
    let rows_before: Vec<String> =
        sqlx::query_scalar("SELECT ctid::text FROM linch_runs ORDER BY run_id")
            .fetch_all(&db.pool)
            .await
            .unwrap();
    let retention = include_str!("../../migrations/0010_retention.sql");
    sqlx::raw_sql(AssertSqlSafe(retention.to_owned()))
        .execute(&db.pool)
        .await
        .unwrap();
    let after: Vec<(String, bool, String)> = sqlx::query_as(
        "SELECT run_id, ended_at IS NOT NULL, ctid::text FROM linch_runs ORDER BY run_id",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert!(after.iter().all(|(_, stamped, _)| !stamped), "{after:?}");
    let places: Vec<String> = after.into_iter().map(|(_, _, ctid)| ctid).collect();
    assert_eq!(places, rows_before, "no row was rewritten");
    let session: bool =
        sqlx::query_scalar("SELECT updated_at > now() - interval '1 minute' FROM linch_sessions")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert!(session);
    db.finish().await;
}
