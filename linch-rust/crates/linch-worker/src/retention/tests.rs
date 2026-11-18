//! The configuration check, and the interleavings a pass must survive: a
//! session or run that changes between the pick and the delete of a batch.
//! The database tests skip cleanly without `LINCH_TEST_PG_DSN`.

use std::str::FromStr;

use sqlx::AssertSqlSafe;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

use super::*;

fn retention() -> Retention {
    Retention {
        terminal_runs: Some(Duration::from_secs(86_400)),
        ..Retention::default()
    }
}

#[test]
fn a_retention_must_delete_something_and_stay_in_range() {
    assert!(retention().check().is_ok());
    let invalid = |change: fn(&mut Retention)| {
        let mut retention = retention();
        change(&mut retention);
        retention.check().is_err()
    };
    assert!(invalid(|r| r.terminal_runs = None), "no window at all");
    assert!(invalid(|r| r.terminal_runs = Some(Duration::from_secs(59))));
    assert!(invalid(|r| r.idle_sessions = Some(Duration::ZERO)));
    assert!(invalid(|r| r.tombstones = Duration::ZERO));
    assert!(invalid(|r| r.batch_size = 0));
    assert!(invalid(|r| r.batch_size = MAX_BATCH_SIZE + 1));
    assert!(invalid(|r| r.interval = Duration::from_secs(59)));
    let idle = Retention {
        idle_sessions: Some(Duration::from_secs(86_400)),
        ..Retention::default()
    };
    assert!(idle.check().is_ok(), "either window is enough");
}

/// A migrated schema `worker_ret_<name>`, or `None` without the DSN.
async fn schema(name: &str) -> Option<(PgPool, PgPool, String)> {
    let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
        eprintln!("skipping {name}: LINCH_TEST_PG_DSN is not set");
        return None;
    };
    let schema = format!("worker_ret_{name}");
    let admin = PgPoolOptions::new().max_connections(1).connect(&dsn).await;
    let admin = admin.expect("connect to the test database");
    let reset = format!("DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}");
    let query = sqlx::raw_sql(AssertSqlSafe(reset));
    query.execute(&admin).await.expect("reset the test schema");
    let options = PgConnectOptions::from_str(&dsn)
        .expect("a valid DSN")
        .options([("search_path", schema.as_str())]);
    let pool = PgPoolOptions::new().max_connections(4);
    let pool = pool.connect_with(options).await.expect("connect");
    linch_postgres::migrate(&pool).await.expect("migrate");
    crate::migrate(&pool).await.expect("worker migrations");
    Some((admin, pool, schema))
}

async fn drop_schema(admin: PgPool, pool: PgPool, schema: String) {
    pool.close().await;
    let drop = format!("DROP SCHEMA {schema} CASCADE");
    sqlx::query(AssertSqlSafe(drop))
        .execute(&admin)
        .await
        .expect("drop the schema");
}

/// A run is created in a session after a batch picked the session and
/// before it deleted: the delete statement sees the run and keeps the
/// session, its entries and its tasks.
#[tokio::test]
async fn a_run_created_after_the_pick_keeps_its_session() {
    let Some((admin, pool, schema)) = schema("race_session").await else {
        return;
    };
    sqlx::query(
        "INSERT INTO linch_sessions (namespace, session_id, len, updated_at) \
         VALUES ('ns', 's', 1, now() - interval '9 days')",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO linch_session_entries (namespace, session_id, position, entry) \
         VALUES ('ns', 's', 0, 'x')",
    )
    .execute(&pool)
    .await
    .unwrap();
    let window = Duration::from_secs(86_400);

    let mut tx = crate::begin(&pool).await.unwrap();
    let picked: Vec<(String, String)> = sqlx::query_as(PICK_SESSIONS)
        .bind(millis(window))
        .bind("")
        .bind("")
        .bind(10_i64)
        .fetch_all(&mut *tx)
        .await
        .unwrap();
    assert_eq!(picked, [("ns".to_owned(), "s".to_owned())]);
    // Another connection starts a run in the picked session and commits.
    sqlx::query(
        "INSERT INTO linch_runs (namespace, run_id, session_id, contract, status, epoch, \
         checkpoint, last_seq) VALUES ('ns', 'r', 's', 'c', 'pending', 0, '{}', 0)",
    )
    .execute(&pool)
    .await
    .unwrap();
    let (namespaces, ids): (Vec<String>, Vec<String>) = picked.into_iter().unzip();
    let (sessions, entries): (i64, i64) = sqlx::query_as(DELETE_SESSIONS)
        .bind(namespaces)
        .bind(ids)
        .bind(millis(window))
        .fetch_one(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert_eq!((sessions, entries), (0, 0));
    let kept: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_session_entries")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(kept, 1);
    drop_schema(admin, pool, schema).await;
}

/// A pick locks its sessions, so an append (which updates the row) waits
/// for the batch instead of landing between its pick and its delete.
#[tokio::test]
async fn a_picked_session_is_locked_against_appends() {
    let Some((admin, pool, schema)) = schema("lock_append").await else {
        return;
    };
    sqlx::query(
        "INSERT INTO linch_sessions (namespace, session_id, len, updated_at) \
         VALUES ('ns', 's', 0, now() - interval '9 days')",
    )
    .execute(&pool)
    .await
    .unwrap();
    let window = Duration::from_secs(86_400);
    let mut tx = crate::begin(&pool).await.unwrap();
    let picked: Vec<(String, String)> = sqlx::query_as(PICK_SESSIONS)
        .bind(millis(window))
        .bind("")
        .bind("")
        .bind(10_i64)
        .fetch_all(&mut *tx)
        .await
        .unwrap();
    assert_eq!(picked.len(), 1);
    let mut other = pool.acquire().await.unwrap();
    sqlx::query("SET lock_timeout = '200ms'")
        .execute(&mut *other)
        .await
        .unwrap();
    let append = sqlx::query(
        "UPDATE linch_sessions SET len = len + 1, updated_at = now() \
         WHERE namespace = 'ns' AND session_id = 's'",
    )
    .execute(&mut *other)
    .await;
    let error = append.expect_err("the picked session is locked against appends");
    let code = error.as_database_error().and_then(|error| error.code());
    assert_eq!(code.as_deref(), Some("55P03"), "{error}");
    drop(other);
    tx.rollback().await.unwrap();
    drop_schema(admin, pool, schema).await;
}
