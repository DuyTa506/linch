//! The worker's own migration table is gated like the store's.

use linch_postgres::SchemaError;
use linch_worker::{MIGRATIONS_TABLE, schema_version};
use sqlx::AssertSqlSafe;

use crate::common::*;

#[tokio::test]
async fn a_worker_schema_ahead_of_the_binary_is_refused() {
    let Some(db) = TestDb::new("schema_newer").await else {
        return;
    };
    let current = schema_version(&db.pool).await.unwrap();
    assert!(current.is_current(), "{current:?}");

    let future = current.latest + 3;
    let sql = format!(
        "INSERT INTO {MIGRATIONS_TABLE} (version, description, success, checksum, execution_time) \
         VALUES ({future}, 'a later release', true, '\\x00', 0)"
    );
    sqlx::query(AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();
    let error = schema_version(&db.pool).await.unwrap_err();
    let SchemaError::Newer { table, applied, .. } = &error else {
        panic!("a newer schema, got {error:?}");
    };
    assert_eq!((table.as_str(), *applied), (MIGRATIONS_TABLE, future));
    db.finish().await;
}
