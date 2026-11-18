//! Explicit migrate and check, and the schema version gate of `agent.open`:
//! a database a newer release migrated is refused before any run is
//! touched. Skipped without `LINCH_TEST_PG_DSN`.

use linch::postgres::{MIGRATIONS_TABLE, SchemaError};
use linch::{AgentConfig, OpenError};
use serde_json::json;
use sqlx::postgres::PgPoolOptions;
use sqlx::{AssertSqlSafe, PgPool};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";

/// A fresh schema `gate_<name>` and a DSN that uses it.
struct TestDb {
    admin: PgPool,
    schema: String,
    dsn: String,
}

impl TestDb {
    async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("gate_{name}");
        let admin = PgPoolOptions::new()
            .max_connections(1)
            .connect(&dsn)
            .await
            .expect("connect to the test database");
        for sql in [
            format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
            format!("CREATE SCHEMA {schema}"),
        ] {
            let query = sqlx::query(AssertSqlSafe(sql));
            query.execute(&admin).await.expect("reset the test schema");
        }
        let separator = if dsn.contains('?') { '&' } else { '?' };
        let dsn = format!("{dsn}{separator}options=-c%20search_path%3D{schema}");
        Some(Self { admin, schema, dsn })
    }

    async fn sql(&self, sql: String) {
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("test SQL");
    }

    fn config(&self) -> AgentConfig {
        serde_json::from_value(json!({
            "namespace": "t",
            "spec": {"model": "scripted-model"},
            "provider": {"id": "scripted", "turns": []},
            "stores": {"type": "postgres", "dsn": self.dsn},
        }))
        .unwrap()
    }

    async fn finish(self) {
        self.sql(format!("DROP SCHEMA {} CASCADE", self.schema))
            .await;
    }
}

#[tokio::test]
async fn check_reads_and_migrate_writes_both_tables() {
    let Some(db) = TestDb::empty("explicit").await else {
        return;
    };
    let before = linch::check(&db.dsn).await.unwrap();
    assert_eq!((before.store.applied, before.worker.applied), (None, None));
    assert!(!before.is_current());
    // A check wrote nothing.
    let tables: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1",
    )
    .bind(&db.schema)
    .fetch_one(&db.admin)
    .await
    .unwrap();
    assert_eq!(tables, 0);

    let migrated = linch::migrate(&db.dsn).await.unwrap();
    assert!(migrated.is_current(), "{migrated:?}");
    assert_eq!(linch::check(&db.dsn).await.unwrap(), migrated);
    assert_eq!(linch::migrate(&db.dsn).await.unwrap(), migrated, "again");
    db.finish().await;
}

#[tokio::test]
async fn an_open_on_a_newer_schema_is_refused_and_a_normal_open_is_not() {
    let Some(db) = TestDb::empty("newer").await else {
        return;
    };
    let normal = db.config().open().await.expect("a normal open migrates");
    normal.close().await;

    let latest = linch::check(&db.dsn).await.unwrap().store.latest;
    let future = latest + 4;
    let schema = &db.schema;
    db.sql(format!(
        "INSERT INTO {schema}.{MIGRATIONS_TABLE} (version, description, success, checksum, execution_time) \
         VALUES ({future}, 'a later release', true, '\\x00', 0)"
    ))
    .await;
    let refused = db.config().open().await.unwrap_err();
    let OpenError::SchemaNewer {
        applied,
        latest: known,
        ..
    } = &refused
    else {
        panic!("a newer schema, got {refused:?}");
    };
    assert_eq!((*applied, *known), (future, latest));
    // The explicit calls say the same, and migrate does not touch it.
    let checked = linch::check(&db.dsn).await.unwrap_err();
    assert!(matches!(checked, SchemaError::Newer { .. }), "{checked:?}");
    let migrated = linch::migrate(&db.dsn).await.unwrap_err();
    assert!(
        matches!(migrated, SchemaError::Newer { .. }),
        "{migrated:?}"
    );
    db.finish().await;
}
