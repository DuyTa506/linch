//! A fresh Postgres schema per test, plus small record builders.
//!
//! Tests read the database from `LINCH_TEST_PG_DSN` and skip without it.
//! Each test gets its own schema (`p7_<name>`, reset at start and dropped at
//! the end) through the pool's `search_path`, so tests run in parallel and
//! the conformance namespaces start empty on every run.

use std::str::FromStr;

use linch_core::events::kinds::ErrorEvent;
use linch_core::{
    Checkpoint, Envelope, Event, JsonObject, Message, NewRun, Phase, RunId, SessionId, Usage,
    WIRE_VERSION,
};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{AssertSqlSafe, PgPool};

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";

pub struct TestDb {
    /// Pool whose `search_path` is the test schema.
    pub pool: PgPool,
    /// Pool without a search path, for test-only SQL.
    pub admin: PgPool,
    /// The test's schema.
    pub schema: String,
}

impl TestDb {
    /// An empty schema, or `None` (with a note) when the DSN is unset.
    pub async fn empty(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p7_{name}");
        let admin = PgPoolOptions::new()
            .max_connections(2)
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
        let options = PgConnectOptions::from_str(&dsn)
            .expect("a valid DSN")
            .options([("search_path", schema.as_str())]);
        let pool = PgPoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .expect("connect to the test schema");
        Some(Self {
            pool,
            admin,
            schema,
        })
    }

    /// A schema with the store tables.
    pub async fn migrated(name: &str) -> Option<Self> {
        let db = Self::empty(name).await?;
        linch_postgres::migrate(&db.pool).await.expect("migrate");
        Some(db)
    }

    /// Close the pools and drop the schema.
    pub async fn finish(self) {
        self.pool.close().await;
        let sql = format!("DROP SCHEMA {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the schema");
        self.admin.close().await;
    }
}

pub fn envelope(run_id: &RunId, seq: u64) -> Envelope {
    let mut error = JsonObject::new();
    error.insert("seq".into(), seq.into());
    Envelope {
        v: WIRE_VERSION,
        seq,
        run_id: run_id.clone(),
        ignorable: false,
        event: Event::Error(ErrorEvent { error }),
    }
}

pub fn checkpoint(turn: u32) -> Checkpoint {
    Checkpoint {
        turn,
        usage: Usage::default(),
        usage_complete: true,
        session_len: 0,
        phase: Phase::Started {
            prompt: Message::user_text("hi"),
        },
    }
}

/// A run in `namespace` with `events` first events, alone in its own
/// session (a session admits one live run).
pub fn new_run(namespace: &str, run_id: &str, events: u64) -> NewRun {
    let session_id = SessionId::new(format!("s-{run_id}"));
    let run_id = RunId::new(run_id);
    NewRun {
        namespace: namespace.into(),
        events: (1..=events).map(|seq| envelope(&run_id, seq)).collect(),
        run_id,
        session_id,
        parent_run_id: None,
        contract: "0".repeat(64).parse().expect("a valid digest"),
        checkpoint: checkpoint(0),
    }
}
