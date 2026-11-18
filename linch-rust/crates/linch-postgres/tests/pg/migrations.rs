//! Migrations are idempotent, safe to race, and keep existing data.

use linch_core::{LogEntry, Message, MessageEntry, SessionId, SessionStore};
use linch_postgres::{MIGRATIONS_TABLE, PgSessionStore, migrate};
use sqlx::AssertSqlSafe;
use sqlx::postgres::PgPoolOptions;

use crate::common::TestDb;

async fn applied(db: &TestDb) -> i64 {
    let sql = format!("SELECT count(*) FROM {MIGRATIONS_TABLE} WHERE success");
    let query = sqlx::query_scalar(AssertSqlSafe(sql));
    query.fetch_one(&db.pool).await.unwrap()
}

#[tokio::test]
async fn migrations_run_twice_are_a_no_op() {
    let Some(db) = TestDb::empty("migrate").await else {
        return;
    };
    let (first, second) = tokio::join!(migrate(&db.pool), migrate(&db.pool));
    first.expect("first of two racing migrations");
    second.expect("second of two racing migrations");
    assert_eq!(applied(&db).await, 10);

    let store = PgSessionStore::new(db.pool.clone());
    let id = SessionId::new("s1");
    store.create("ns", &id).await.unwrap();
    let entry = LogEntry::Message(MessageEntry {
        message: Message::user_text("kept").into(),
        visible: true,
        historical: true,
    });
    store
        .append("ns", &id, 0, vec![entry.clone()])
        .await
        .unwrap();

    migrate(&db.pool).await.expect("migrate again");
    assert_eq!(applied(&db).await, 10, "nothing re-applied");
    assert_eq!(store.load("ns", &id).await.unwrap(), [entry], "data kept");
    let default: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('_sqlx_migrations')::text")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(default, None, "sqlx's default table is left to the host");
    db.finish().await;
}

#[tokio::test]
async fn a_schema_migrates_while_another_schemas_migration_is_stuck() {
    let (Some(held), Some(free)) = (
        TestDb::empty("migrate_lock_held").await,
        TestDb::empty("migrate_lock_free").await,
    ) else {
        return;
    };
    // Stand in for a migration that never finishes: hold the held schema's lock.
    let mut holder = held.pool.acquire().await.unwrap();
    sqlx::query(
        "SELECT pg_advisory_lock(hashtextextended(\
         'linch_migrate:' || current_database() || ':' || current_schema(), 0))",
    )
    .execute(&mut *holder)
    .await
    .unwrap();

    let wait = std::time::Duration::from_secs(20);
    let migrated = tokio::time::timeout(wait, migrate(&free.pool)).await;
    migrated.expect("not blocked by the other schema").unwrap();

    let blocked = std::time::Duration::from_millis(500);
    let same = tokio::time::timeout(blocked, migrate(&held.pool)).await;
    assert!(same.is_err(), "the held schema's own migration waits");

    drop(holder);
    held.finish().await;
    free.finish().await;
}

/// Hold `db`'s schema's migration lock on a connection, as a migration that
/// is still running does.
async fn hold_the_lock(db: &TestDb) -> sqlx::pool::PoolConnection<sqlx::Postgres> {
    let mut holder = db.pool.acquire().await.unwrap();
    sqlx::query(
        "SELECT pg_advisory_lock(hashtextextended(\
         'linch_migrate:' || current_database() || ':' || current_schema(), 0))",
    )
    .execute(&mut *holder)
    .await
    .unwrap();
    holder
}

/// A schema that is fully migrated is only read: it does not wait for the
/// migration lock, which a slow migration elsewhere may hold for long.
#[tokio::test]
async fn a_migrated_schema_does_not_wait_for_the_lock() {
    let Some(db) = TestDb::migrated("migrate_read_only").await else {
        return;
    };
    let holder = hold_the_lock(&db).await;
    let wait = std::time::Duration::from_secs(5);
    let migrated = tokio::time::timeout(wait, migrate(&db.pool)).await;
    migrated.expect("an applied schema takes no lock").unwrap();
    drop(holder);
    db.finish().await;
}

/// A migration that is missing still waits for the lock and runs: the last
/// one is taken out of the table, and only that one is applied again.
#[tokio::test]
async fn a_missing_migration_takes_the_lock_and_runs() {
    let Some(db) = TestDb::migrated("migrate_missing").await else {
        return;
    };
    // Undo migration 10, as a database a release behind has it.
    let undo = format!(
        "DELETE FROM {MIGRATIONS_TABLE} WHERE version = 10; \
         ALTER TABLE linch_runs DROP COLUMN ended_at; \
         ALTER TABLE linch_sessions DROP COLUMN updated_at; \
         DROP TABLE linch_pruned_runs"
    );
    let undone = sqlx::raw_sql(AssertSqlSafe(undo)).execute(&db.pool).await;
    undone.unwrap();
    let holder = hold_the_lock(&db).await;
    let blocked = std::time::Duration::from_millis(500);
    let waits = tokio::time::timeout(blocked, migrate(&db.pool)).await;
    assert!(waits.is_err(), "a missing migration waits for the lock");
    // The lock belongs to the session, which the pool keeps: let it go.
    let mut holder = holder;
    let unlock = sqlx::query("SELECT pg_advisory_unlock_all()");
    unlock.execute(&mut *holder).await.unwrap();
    drop(holder);
    migrate(&db.pool).await.unwrap();
    assert_eq!(applied(&db).await, 10);
    db.finish().await;
}

/// A pool whose `search_path` is `path`.
async fn on(db: &TestDb, path: String) -> sqlx::PgPool {
    let options = db.options.clone().options([("search_path", path)]);
    let pool = PgPoolOptions::new().max_connections(2);
    pool.connect_with(options).await.unwrap()
}

/// The applied migrations are those of the schema the connection uses, not
/// those of another schema on its `search_path`: with the schema after it
/// migrated and the connection's own schema empty, `migrate` still migrates
/// the own one.
#[tokio::test]
async fn another_schemas_migrations_do_not_count() {
    let Some(db) = TestDb::empty("migrate_path").await else {
        return;
    };
    let other = format!("{}_other", db.schema);
    let reset = format!("DROP SCHEMA IF EXISTS {other} CASCADE; CREATE SCHEMA {other}");
    sqlx::raw_sql(AssertSqlSafe(reset))
        .execute(&db.admin)
        .await
        .unwrap();
    let migrated = on(&db, other.clone()).await;
    migrate(&migrated).await.unwrap();
    let both = on(&db, format!("{},{other}", db.schema)).await;
    migrate(&both).await.unwrap();
    assert_eq!(applied(&db).await, 10, "the own schema was migrated");
    both.close().await;
    migrated.close().await;
    let cleanup = format!("DROP SCHEMA {other} CASCADE");
    sqlx::raw_sql(AssertSqlSafe(cleanup))
        .execute(&db.admin)
        .await
        .unwrap();
    db.finish().await;
}

/// An applied migration that differs from the embedded one is not "all
/// applied": the locked path reports it, as before.
#[tokio::test]
async fn a_changed_migration_is_still_refused() {
    let Some(db) = TestDb::migrated("migrate_changed").await else {
        return;
    };
    let change = format!("UPDATE {MIGRATIONS_TABLE} SET checksum = '\\x00' WHERE version = 1");
    sqlx::query(AssertSqlSafe(change))
        .execute(&db.pool)
        .await
        .unwrap();
    let refused = migrate(&db.pool).await;
    assert!(
        matches!(
            refused,
            Err(linch_postgres::MigrateError::VersionMismatch(1))
        ),
        "{refused:?}"
    );
    db.finish().await;
}
