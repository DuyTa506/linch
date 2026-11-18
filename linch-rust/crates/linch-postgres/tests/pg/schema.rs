//! The schema version gate: what the database holds against what this
//! binary knows, read without writing anything.

use linch_postgres::{MIGRATIONS_TABLE, SchemaError, migrate, schema_version};
use sqlx::AssertSqlSafe;

use crate::common::TestDb;

#[tokio::test]
async fn an_unmigrated_schema_has_no_applied_version_and_a_migrated_one_is_current() {
    let Some(db) = TestDb::empty("schema_version").await else {
        return;
    };
    let before = schema_version(&db.pool).await.expect("an empty schema");
    assert_eq!(before.applied, None);
    assert!(before.latest >= 10, "{before:?}");
    assert!(!before.is_current());

    migrate(&db.pool).await.unwrap();
    let after = schema_version(&db.pool).await.unwrap();
    assert_eq!(after.applied, Some(after.latest));
    assert!(after.is_current());
    db.finish().await;
}

#[tokio::test]
async fn a_schema_ahead_of_the_binary_is_refused_with_the_versions() {
    let Some(db) = TestDb::migrated("schema_newer").await else {
        return;
    };
    let current = schema_version(&db.pool).await.unwrap();
    let future = current.latest + 5;
    let sql = format!(
        "INSERT INTO {MIGRATIONS_TABLE} (version, description, success, checksum, execution_time) \
         VALUES ({future}, 'a later release', true, '\\x00', 0)"
    );
    sqlx::query(AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();

    let error = schema_version(&db.pool).await.unwrap_err();
    let SchemaError::Newer {
        table,
        applied,
        latest,
    } = &error
    else {
        panic!("a newer schema, got {error:?}");
    };
    assert_eq!(
        (table.as_str(), *applied, *latest),
        (MIGRATIONS_TABLE, future, current.latest)
    );
    let text = error.to_string();
    assert!(
        text.contains("newer") && text.contains(&future.to_string()),
        "{text}"
    );
    db.finish().await;
}

#[tokio::test]
async fn a_dirty_migration_is_not_counted_as_applied() {
    let Some(db) = TestDb::migrated("schema_dirty").await else {
        return;
    };
    let latest = schema_version(&db.pool).await.unwrap().latest;
    let sql = format!("UPDATE {MIGRATIONS_TABLE} SET success = false WHERE version = {latest}");
    sqlx::query(AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();
    let version = schema_version(&db.pool).await.unwrap();
    assert_eq!(version.applied, Some(latest - 1));
    db.finish().await;
}
