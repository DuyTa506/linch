//! Migrations are idempotent, safe to race, and keep existing data.

use linch_core::{LogEntry, Message, MessageEntry, SessionId, SessionStore};
use linch_postgres::{MIGRATIONS_TABLE, PgSessionStore, migrate};
use sqlx::AssertSqlSafe;

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
    assert_eq!(applied(&db).await, 4);

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
    assert_eq!(applied(&db).await, 4, "nothing re-applied");
    assert_eq!(store.load("ns", &id).await.unwrap(), [entry], "data kept");
    let default: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('_sqlx_migrations')::text")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(default, None, "sqlx's default table is left to the host");
    db.finish().await;
}
