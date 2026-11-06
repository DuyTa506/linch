//! Shared helpers: a fresh schema per test, run and job queries, workers
//! on the test runtime, and waiting for state. The agent under test is in
//! `fixtures`, re-exported here.
//!
//! Tests read the database from `LINCH_TEST_PG_DSN` and skip without it.
//! Each test gets its own schema `p9_<name>` (reset at start, dropped at the
//! end) through the pool's `search_path`. `NOTIFY` channels are per
//! database, so parallel tests wake each other's workers; that is harmless.

#![allow(dead_code)]

use std::future::Future;
use std::str::FromStr;
use std::time::Duration;

use linch_core::{
    Agent, ContentBlock, Envelope, LogEntry, RunId, RunRecord, RunStatus, RunStore, SessionId,
    SessionStore, ToolResultPart,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use linch_worker::{Worker, WorkerConfig, WorkerError};
use sqlx::AssertSqlSafe;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use tokio::task::JoinHandle;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

pub use crate::fixtures::*;

const DSN_VAR: &str = "LINCH_TEST_PG_DSN";
pub const NS: &str = "p9";
/// Visibility timeout of a worker that a test means to lose its job (it is
/// killed or stalls), so another worker takes the job over soon. A worker
/// drops its run when no heartbeat lands within its timeout and then claims
/// the run again, which a test counting claims must not see; 3 s holds
/// before the kill even when one heartbeat of a loaded database is slow.
pub const TIMEOUT: Duration = Duration::from_secs(3);
/// Visibility timeout of every other test worker, so that no slow
/// heartbeat ever makes it drop and claim its run again.
pub const HOLD: Duration = Duration::from_secs(30);

pub struct TestDb {
    /// Pool whose `search_path` is the test schema.
    pub pool: PgPool,
    /// Connect options of `pool`, for a pool on another runtime.
    pub options: PgConnectOptions,
    admin: PgPool,
    schema: String,
}

impl TestDb {
    /// A migrated empty schema, or `None` (with a note) without the DSN.
    pub async fn new(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var(DSN_VAR) else {
            eprintln!("skipping {name}: {DSN_VAR} is not set");
            return None;
        };
        let schema = format!("p9_{name}");
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
            .max_connections(16)
            .connect_with(options.clone())
            .await
            .expect("connect to the test schema");
        linch_postgres::migrate(&pool)
            .await
            .expect("store migrations");
        linch_worker::migrate(&pool)
            .await
            .expect("worker migrations");
        Some(Self {
            pool,
            options,
            admin,
            schema,
        })
    }

    /// Close the pools and drop the schema.
    pub async fn finish(self) {
        self.pool.close().await;
        let sql = format!("DROP SCHEMA {} CASCADE", self.schema);
        let query = sqlx::query(AssertSqlSafe(sql));
        query.execute(&self.admin).await.expect("drop the schema");
        self.admin.close().await;
    }

    pub async fn record(&self, run_id: &RunId) -> RunRecord {
        PgRunStore::new(self.pool.clone())
            .load(NS, run_id)
            .await
            .expect("load the run")
    }

    pub async fn events(&self, run_id: &RunId) -> Vec<Envelope> {
        PgRunStore::new(self.pool.clone())
            .events(NS, run_id, 0)
            .await
            .expect("load the events")
    }

    /// Runs of `session`, oldest first (test-only SQL on the store table).
    pub async fn runs_of(&self, session: &str) -> Vec<RunId> {
        let ids: Vec<String> = sqlx::query_scalar(
            "SELECT run_id FROM linch_runs WHERE namespace = $1 AND session_id = $2 \
             ORDER BY run_id",
        )
        .bind(NS)
        .bind(session)
        .fetch_all(&self.pool)
        .await
        .expect("list runs");
        ids.into_iter().map(RunId::new).collect()
    }

    /// The single run of `session`, once it exists.
    pub async fn run_of(&self, session: &str) -> RunId {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            let mut found = self.runs_of(session).await;
            if !found.is_empty() {
                assert_eq!(found.len(), 1, "one run in {session}");
                return found.remove(0);
            }
            assert!(Instant::now() < deadline, "timed out waiting for a run");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    /// The text of every tool result in `session`'s log, in order.
    pub async fn tool_results(&self, session: &str) -> Vec<String> {
        let sessions = PgSessionStore::new(self.pool.clone());
        let entries = (sessions.load(NS, &SessionId::new(session)).await).expect("load");
        let mut texts = Vec::new();
        for entry in entries {
            let LogEntry::Message(entry) = entry else {
                continue;
            };
            for block in &entry.message.content {
                if let ContentBlock::ToolResult(result) = block {
                    for part in &result.content {
                        if let ToolResultPart::Text(text) = part {
                            texts.push(text.text.clone());
                        }
                    }
                }
            }
        }
        texts
    }

    pub async fn status(&self, run_id: &RunId) -> RunStatus {
        self.record(run_id).await.status
    }

    /// How long the only job stays hidden from now, by the database clock
    /// (negative once it is visible).
    pub async fn hidden_for(&self) -> Duration {
        let millis: i64 = sqlx::query_scalar(
            "SELECT (extract(epoch FROM visible_at - now()) * 1000)::bigint \
             FROM linch_worker_jobs",
        )
        .fetch_one(&self.pool)
        .await
        .expect("read the job's visibility");
        Duration::from_millis(u64::try_from(millis).expect("the job is still hidden"))
    }

    pub async fn jobs(&self) -> i64 {
        sqlx::query_scalar("SELECT count(*) FROM linch_worker_jobs")
            .fetch_one(&self.pool)
            .await
            .expect("count jobs")
    }

    pub async fn sql(&self, sql: &'static str) {
        sqlx::query(sql)
            .execute(&self.pool)
            .await
            .expect("test SQL");
    }
}

/// Poll `check` until it holds; fail after 20 s.
pub async fn wait_until<F, Fut>(what: &str, mut check: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = bool>,
{
    let deadline = Instant::now() + Duration::from_secs(20);
    while !check().await {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// Fast settings for tests: `HOLD` visibility, 50 ms poll, 200 ms retry.
pub fn config(owner: &str) -> WorkerConfig {
    let mut config = WorkerConfig::new(owner);
    config.queues = vec!["default".into()];
    config.max_jobs = 4;
    config.visibility_timeout = HOLD;
    config.poll_interval = Duration::from_millis(50);
    config.retry_delay = Duration::from_millis(200);
    config
}

/// [`config`] for a worker that the test kills or stalls: its job becomes
/// visible to the next worker soon after.
pub fn losing(owner: &str) -> WorkerConfig {
    let mut config = config(owner);
    config.visibility_timeout = TIMEOUT;
    config
}

/// A worker serving on the current runtime.
pub struct Running {
    shutdown: CancellationToken,
    handle: JoinHandle<Result<(), WorkerError>>,
}

impl Running {
    pub fn spawn(pool: &PgPool, config: WorkerConfig, agent: Agent) -> Self {
        let shutdown = CancellationToken::new();
        let worker = Worker::new(pool.clone(), config).agent(agent);
        let handle = tokio::spawn(worker.run(shutdown.clone()));
        Self { shutdown, handle }
    }

    /// Stop it cleanly: no new jobs, wait for the jobs in flight.
    pub async fn stop(self) {
        self.shutdown.cancel();
        self.handle
            .await
            .expect("the worker task")
            .expect("the worker");
    }

    /// Abort it like a crash: no ack, no release, no more heartbeats.
    pub async fn kill(self) {
        self.handle.abort();
        assert!(self.handle.await.unwrap_err().is_cancelled());
    }
}
