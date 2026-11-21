//! A fleet whose database connections are killed at random
//! (`pg_terminate_backend`) while it runs jobs: every job still ends, each
//! prompt is one run, and no tool runs twice.

use std::time::Duration;

use linch_core::{RunStatus, SessionId, ToolScope};
use linch_postgres::PgPool;
use linch_worker::enqueue_run;
use sqlx::postgres::PgPoolOptions;
use tokio::time::Instant;

use crate::common::*;

/// A pool like `linch_postgres::connect` makes (no ping before every
/// acquire, a ping after a second idle), tagged so the test can find its
/// backends.
async fn tagged_pool(db: &TestDb, tag: &str) -> PgPool {
    PgPoolOptions::new()
        .test_before_acquire(false)
        .before_acquire(|connection, meta| {
            Box::pin(async move {
                if meta.idle_for >= Duration::from_secs(1) {
                    sqlx::Connection::ping(connection).await?;
                }
                Ok(true)
            })
        })
        .max_connections(8)
        .connect_with(db.options.clone().application_name(tag))
        .await
        .expect("connect a tagged pool")
}

/// Terminate each backend tagged `tag` with probability `share`; how many.
async fn terminate(db: &TestDb, tag: &str, share: f64) -> i64 {
    sqlx::query_scalar(
        "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE application_name = $1 AND datname = current_database() \
           AND random() < $2) AS killed",
    )
    .bind(tag)
    .bind(share)
    .fetch_one(&db.pool)
    .await
    .expect("terminate backends")
}

async fn count(db: &TestDb, sql: &'static str) -> i64 {
    sqlx::query_scalar(sql)
        .fetch_one(&db.pool)
        .await
        .expect("count")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_fleet_whose_connections_are_killed_runs_each_job_once() {
    let Some(db) = TestDb::new("faults").await else {
        return;
    };
    let tag = format!("faults_{}", std::process::id());
    let probe = Probe::new(
        "Lookup",
        ToolScope::Read,
        Hold::Sleep(Duration::from_millis(1000)),
    );
    let sessions: Vec<String> = (0..60).map(|i| format!("s{i}")).collect();
    for session in &sessions {
        let session_id = SessionId::new(session.as_str());
        enqueue_run(&db.pool, "default", NS, &session_id, "go")
            .await
            .unwrap();
    }
    let mut workers = Vec::new();
    let mut pools = Vec::new();
    for name in ["w1", "w2", "w3"] {
        let pool = tagged_pool(&db, &tag).await;
        let mut config = losing(name);
        config.max_jobs = 3;
        workers.push(Running::spawn(&pool, config, agent(&pool, probe.clone())));
        pools.push(pool);
    }
    // Faults for a while, then a healthy database until every job ended.
    let faults_until = Instant::now() + Duration::from_secs(8);
    let mut killed = 0;
    while Instant::now() < faults_until {
        killed += terminate(&db, &tag, 0.2).await;
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let live = count(
            &db,
            "SELECT count(*) FROM linch_worker_jobs WHERE failed_at IS NULL",
        )
        .await;
        if live == 0 {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "{live} jobs never ended after {killed} killed connections"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let failed = count(&db, "SELECT count(*) FROM linch_worker_jobs").await;
    eprintln!("killed {killed} connections; {failed} jobs failed for good");

    // Each prompt is one run, and every run ended.
    let mut completed = 0;
    for session in &sessions {
        let runs = db.runs_of(session).await;
        assert_eq!(runs.len(), 1, "{session} has runs {runs:?}");
        let status = db.status(&runs[0]).await;
        assert!(status.is_terminal(), "{session}: {status:?}");
        if status == RunStatus::Completed {
            completed += 1;
            let results = db.tool_results(session).await;
            assert_eq!(results.len(), 1, "{session}: {results:?}");
            let entries = entries_of(&db, session).await;
            // The prefix, the prompt, the call, its result and the answer.
            assert_eq!(
                entries.len(),
                5,
                "{session} log: {entries:#?}; events: {:#?}",
                events_of(&db, session).await
            );
        }
    }
    assert_eq!(
        i64::try_from(sessions.len()).unwrap() - failed,
        completed,
        "a job that did not fail completed its run"
    );
    {
        let stats = probe.stats.lock().unwrap();
        let twice: Vec<_> = stats.runs.iter().filter(|(_, calls)| **calls > 1).collect();
        assert!(twice.is_empty(), "tools ran twice: {twice:?}");
    }
    for worker in workers {
        tokio::time::timeout(Duration::from_secs(60), worker.stop())
            .await
            .expect("the worker stops");
    }
    for pool in pools {
        pool.close().await;
    }
    db.finish().await;
}

async fn entries_of(db: &TestDb, session: &str) -> Vec<String> {
    sqlx::query_scalar(
        "SELECT left(entry, 400) FROM linch_session_entries \
         WHERE namespace = $1 AND session_id = $2 ORDER BY position",
    )
    .bind(NS)
    .bind(session)
    .fetch_all(&db.pool)
    .await
    .expect("read entries")
}

async fn events_of(db: &TestDb, session: &str) -> Vec<String> {
    sqlx::query_scalar(
        "SELECT e.seq || ' ' || left(e.envelope, 330) FROM linch_run_events e JOIN linch_runs r \
         ON r.namespace = e.namespace AND r.run_id = e.run_id \
         WHERE r.namespace = $1 AND r.session_id = $2 ORDER BY e.seq",
    )
    .bind(NS)
    .bind(session)
    .fetch_all(&db.pool)
    .await
    .expect("read events")
}

/// Eight servers that start together, each on its own pool, on a schema
/// that is empty and then on one that is a migration behind: every start
/// succeeds and each migration is applied once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn servers_starting_together_migrate_once() {
    let Some(db) = TestDb::new("starts").await else {
        return;
    };
    let schema = format!("p9_{}_starts_empty", std::process::id());
    for sql in [
        format!("DROP SCHEMA IF EXISTS {schema} CASCADE"),
        format!("CREATE SCHEMA {schema}"),
    ] {
        let query = sqlx::query(sqlx::AssertSqlSafe(sql));
        query.execute(&db.pool).await.unwrap();
    }
    let options = db
        .options
        .clone()
        .options([("search_path", schema.as_str())]);
    let mut pools = Vec::new();
    for _ in 0..8 {
        let pool = PgPoolOptions::new().max_connections(2);
        pools.push(pool.connect_with(options.clone()).await.unwrap());
    }
    let start = |pool: PgPool| async move {
        linch_postgres::migrate(&pool)
            .await
            .map_err(|error| error.to_string())?;
        linch_worker::migrate(&pool)
            .await
            .map_err(|error| error.to_string())
    };
    let applied = |pool: PgPool| async move {
        let stores: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_migrations WHERE success")
            .fetch_one(&pool)
            .await
            .unwrap();
        let queue: i64 =
            sqlx::query_scalar("SELECT count(*) FROM linch_worker_migrations WHERE success")
                .fetch_one(&pool)
                .await
                .unwrap();
        (stores, queue)
    };
    let together = || async {
        let starts: Vec<_> = pools.iter().cloned().map(start).map(tokio::spawn).collect();
        let mut errors = Vec::new();
        for started in starts {
            if let Err(error) = started.await.unwrap() {
                errors.push(error);
            }
        }
        errors
    };
    let errors = together().await;
    assert!(errors.is_empty(), "on an empty schema: {errors:#?}");
    let all = applied(pools[0].clone()).await;
    assert_eq!(all, (10, 7));
    // One migration behind in both tables.
    for sql in [
        "DELETE FROM linch_migrations WHERE version = 10",
        "DROP TABLE linch_pruned_runs",
        "ALTER TABLE linch_runs DROP COLUMN ended_at",
        "ALTER TABLE linch_sessions DROP COLUMN updated_at",
        "DELETE FROM linch_worker_migrations WHERE version = 7",
        "DROP INDEX linch_worker_history_run",
        "ALTER TABLE linch_worker_history DROP COLUMN agent",
    ] {
        sqlx::query(sql).execute(&pools[0]).await.unwrap();
    }
    assert_eq!(applied(pools[0].clone()).await, (9, 6));
    let errors = together().await;
    assert!(errors.is_empty(), "a migration behind: {errors:#?}");
    assert_eq!(applied(pools[0].clone()).await, all);
    for pool in pools {
        pool.close().await;
    }
    let drop = format!("DROP SCHEMA {schema} CASCADE");
    let query = sqlx::query(sqlx::AssertSqlSafe(drop));
    query.execute(&db.pool).await.unwrap();
    db.finish().await;
}

/// Whether the run of `session` completes within `within`.
async fn completes(db: &TestDb, session: &str, within: Duration) -> bool {
    let deadline = Instant::now() + within;
    loop {
        for run_id in db.runs_of(session).await {
            if db.status(&run_id).await == RunStatus::Completed {
                return true;
            }
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// A trigger on the test schema's job table that waits, inside the statement
/// `on` (`INSERT` or `UPDATE`) when `when` holds, for an advisory lock the
/// returned transaction holds: the transaction of the writer it catches
/// stays open until the caller commits the gate, as that of a process that
/// paused or lost its network in the middle of it. The wait ignores the
/// writer's own `lock_timeout`.
async fn slow_write(
    db: &TestDb,
    on: &str,
    when: &str,
) -> sqlx::Transaction<'static, sqlx::Postgres> {
    const WAIT: &str = "pg_advisory_xact_lock(hashtextextended(current_schema(), 0))";
    let sql = format!(
        "CREATE FUNCTION slow_write() RETURNS trigger LANGUAGE plpgsql SET lock_timeout = 0 AS $$ \
         BEGIN IF {when} THEN PERFORM {WAIT}; END IF; RETURN NEW; END $$; \
         CREATE TRIGGER slow_write BEFORE {on} ON linch_worker_jobs \
         FOR EACH ROW EXECUTE FUNCTION slow_write();"
    );
    sqlx::raw_sql(sqlx::AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();
    let mut gate = db.pool.begin().await.unwrap();
    sqlx::query(sqlx::AssertSqlSafe(format!("SELECT {WAIT}")))
        .execute(&mut *gate)
        .await
        .unwrap();
    gate
}

/// Wait until a backend is blocked on a lock `gate` holds.
async fn wait_until_blocked_on(gate: &mut sqlx::Transaction<'static, sqlx::Postgres>) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let blocked: bool = sqlx::query_scalar(
            // pg_locks, unlike pg_stat_activity, is not a snapshot per
            // transaction, and the gate's transaction is a long one.
            "SELECT EXISTS (SELECT 1 FROM pg_locks \
             WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid)))",
        )
        .fetch_one(&mut **gate)
        .await
        .unwrap();
        if blocked {
            return;
        }
        assert!(Instant::now() < deadline, "no writer waited for the gate");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// A capped `enqueue` that is slow inside its transaction (it holds its
/// root's cap lock meanwhile) must not stop a worker: a job that has
/// nothing to do with that enqueue still runs.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_slow_capped_enqueue_does_not_stop_a_worker() {
    let Some(db) = TestDb::new("slow_cap").await else {
        return;
    };
    let root = format!("capped{}", std::process::id());
    let cap = linch_worker::Cap {
        root: root.clone(),
        max: 100,
    };
    // The worker has a due schedule under the root, so its next poll fires
    // it, under the root's cap.
    let schedule = linch_worker::Schedule {
        namespace: format!("{root}/a"),
        id: "tick".into(),
        queue: "default".into(),
        session_id: None,
        prompt: "go".into(),
        timing: linch_worker::Timing::Every(Duration::from_secs(1)),
        agent: None,
        principal: None,
    };
    linch_worker::create_schedule(&db.pool, &schedule, None)
        .await
        .unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let mut gate = slow_write(&db, "INSERT", "NEW.session_id = 'slow'").await;
    let request = linch_worker::RunRequest {
        queue: "default".into(),
        namespace: format!("{root}/a"),
        session_id: SessionId::new("slow"),
        prompt: "go".into(),
        agent: None,
        principal: None,
    };
    let (pool, held) = (db.pool.clone(), cap.clone());
    let enqueue =
        tokio::spawn(async move { linch_worker::enqueue(&pool, &request, Some(&held)).await });
    wait_until_blocked_on(&mut gate).await;

    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = linch_worker::Worker::new(db.pool.clone(), config("w1"))
        .agent(agent(&db.pool, probe))
        .job_caps([cap]);
    let stop = tokio_util::sync::CancellationToken::new();
    let serving = tokio::spawn(worker.run(stop.clone()));
    let ran = completes(&db, "s1", Duration::from_secs(6)).await;
    gate.commit().await.unwrap();
    enqueue.await.unwrap().expect("the slow enqueue lands");
    let ran_after = completes(&db, "s1", Duration::from_secs(20)).await;
    stop.cancel();
    serving.await.unwrap().unwrap();
    db.finish().await;
    assert!(ran_after, "the job runs once the enqueue is done");
    assert!(
        ran,
        "the worker ran no job for 6 s while a capped enqueue of another root was in flight"
    );
}

/// The same for a queue with a limit: a worker that is slow inside a
/// dequeue of that queue (it holds the queue's lock meanwhile) must not
/// stop another worker from serving its other queues.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_slow_dequeue_of_a_limited_queue_does_not_stop_the_other_queues() {
    let Some(db) = TestDb::new("slow_queue").await else {
        return;
    };
    let limited = format!("limited{}", std::process::id());
    linch_worker::set_queue_limit(&db.pool, &limited, 4)
        .await
        .unwrap();
    enqueue_run(&db.pool, &limited, NS, &SessionId::new("slow"), "go")
        .await
        .unwrap();
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let mut gate = slow_write(
        &db,
        "UPDATE",
        "NEW.attempts > OLD.attempts AND NEW.session_id = 'slow'",
    )
    .await;
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let mut slow = config("w-slow");
    slow.queues = vec![limited.clone()];
    let slow = Running::spawn(&db.pool, slow, agent(&db.pool, probe.clone()));
    wait_until_blocked_on(&mut gate).await;

    let mut config = config("w1");
    config.queues = vec![limited, "default".into()];
    let worker = Running::spawn(&db.pool, config, agent(&db.pool, probe));
    let ran = completes(&db, "s1", Duration::from_secs(6)).await;
    gate.commit().await.unwrap();
    let ran_after = completes(&db, "s1", Duration::from_secs(30)).await
        && completes(&db, "slow", Duration::from_secs(30)).await;
    worker.stop().await;
    slow.stop().await;
    db.finish().await;
    assert!(ran_after, "both jobs run once the slow dequeue is done");
    assert!(
        ran,
        "the worker served no queue for 6 s while another worker's dequeue of one queue was in flight"
    );
}
