//! The fenced writes on Postgres under a REPEATABLE READ default. Each test
//! skips cleanly without `LINCH_TEST_PG_DSN`.

use std::future::Future;
use std::str::FromStr;
use std::time::{Duration, Instant};

use linch_core::{RunId, SessionId};
use sqlx::AssertSqlSafe;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

use super::*;
use crate::enqueue_run;

/// A migrated schema, with a pool at the server's default isolation and
/// one whose transactions default to REPEATABLE READ.
struct Db {
    admin: PgPool,
    pool: PgPool,
    repeatable_read: PgPool,
    schema: String,
}

impl Db {
    async fn new(name: &str) -> Option<Self> {
        let Ok(dsn) = std::env::var("LINCH_TEST_PG_DSN") else {
            eprintln!("skipping {name}: LINCH_TEST_PG_DSN is not set");
            return None;
        };
        let schema = format!("worker_{name}");
        let admin = PgPoolOptions::new().max_connections(1).connect(&dsn).await;
        let admin = admin.expect("connect to the test database");
        let reset = format!("DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}");
        let query = sqlx::raw_sql(AssertSqlSafe(reset));
        query.execute(&admin).await.expect("reset the test schema");
        let options = PgConnectOptions::from_str(&dsn)
            .expect("a valid DSN")
            .options([("search_path", schema.as_str())]);
        let connect = |options: PgConnectOptions| async {
            let pool = PgPoolOptions::new().max_connections(2);
            pool.connect_with(options).await.expect("connect")
        };
        let pool = connect(options.clone()).await;
        let isolation = ("default_transaction_isolation", "repeatable read");
        let repeatable_read = connect(options.options([isolation])).await;
        crate::migrate(&pool).await.expect("worker migrations");
        Some(Self {
            admin,
            pool,
            repeatable_read,
            schema,
        })
    }

    /// A job of its own `queue`, dequeued on the REPEATABLE READ pool.
    async fn held(&self, queue: &str) -> JobLease {
        let session = SessionId::new(queue);
        let enqueued = enqueue_run(&self.pool, queue, "ns", &session, "go").await;
        enqueued.expect("enqueue");
        let namespaces = ["ns".to_owned()];
        let timeout = Duration::from_secs(30);
        let lease = dequeue(&self.repeatable_read, queue, &namespaces, "w", timeout).await;
        lease.expect("dequeue").expect("a visible job")
    }

    /// Run `write` while another transaction holds an update of `lease`'s
    /// job row (the attempt's own heartbeat still in flight, say), and
    /// commit that transaction once `write` waits on it.
    async fn behind_an_update<T>(&self, lease: &JobLease, write: impl Future<Output = T>) -> T {
        let mut holder = self.pool.begin().await.unwrap();
        let updated = sqlx::query("UPDATE linch_worker_jobs SET error = error WHERE id = $1")
            .bind(lease.job.id.0)
            .execute(&mut *holder)
            .await
            .unwrap();
        assert_eq!(updated.rows_affected(), 1);
        let release = async {
            let deadline = Instant::now() + Duration::from_secs(10);
            // pg_locks, unlike pg_stat_activity, is not a snapshot per
            // transaction.
            while sqlx::query_scalar::<_, i32>(
                "SELECT pid FROM pg_locks \
                 WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))",
            )
            .fetch_optional(&mut *holder)
            .await
            .unwrap()
            .is_none()
            {
                assert!(Instant::now() < deadline, "the write never waited");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            holder.commit().await.unwrap();
        };
        let (result, ()) = tokio::join!(write, release);
        result
    }

    async fn finish(self) {
        self.pool.close().await;
        self.repeatable_read.close().await;
        let drop = format!("DROP SCHEMA IF EXISTS {} CASCADE", self.schema);
        let query = sqlx::raw_sql(AssertSqlSafe(drop));
        query
            .execute(&self.admin)
            .await
            .expect("drop the test schema");
    }
}

/// A fenced write that waited on another update of its job row lands once
/// that update commits. At REPEATABLE READ the wait ends in a serialization
/// error, which the write runs again; under READ COMMITTED it never does.
#[tokio::test]
async fn under_repeatable_read_a_fenced_write_behind_an_update_lands() {
    let Some(db) = Db::new("rr_fenced").await else {
        return;
    };
    let pool = &db.repeatable_read;
    let timeout = Duration::from_secs(30);
    let lease = db.held("heartbeat").await;
    let done = db.behind_an_update(&lease, lease.heartbeat(pool, timeout));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "heartbeat: {done:?}");
    let lease = db.held("record_run").await;
    let run_id = RunId::new("r1");
    let done = db.behind_an_update(&lease, lease.record_run(pool, &run_id));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "record_run: {done:?}");
    let lease = db.held("ack").await;
    let done = db.behind_an_update(&lease, lease.ack(pool)).await;
    assert!(matches!(done, Ok(true)), "ack: {done:?}");
    let lease = db.held("release").await;
    let done = db.behind_an_update(&lease, lease.release(pool, timeout, "error", true));
    let done = done.await;
    assert!(matches!(done, Ok(true)), "release: {done:?}");
    let lease = db.held("fail").await;
    let done = db.behind_an_update(&lease, lease.fail(pool, "error")).await;
    assert!(matches!(done, Ok(true)), "fail: {done:?}");
    db.finish().await;
}
