//! Retention: what a pass deletes, what it never touches, and what a client
//! sees of a run it deleted. Rows are seeded with test-only SQL so that a
//! test sets any age or status it needs.

use std::sync::Arc;
use std::time::Duration;

use linch_core::{
    LogEntry, Message, MessageEntry, RunId, RunStatus, RunStore, SessionId, SessionStore,
    StoreError, ToolScope,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use linch_worker::{
    Retention, Worker, enqueue_approve, enqueue_run, fire_due_schedules, get_schedule, job, jobs,
    prune_retained,
};
use tokio_util::sync::CancellationToken;

use crate::common::*;
use crate::schedule::{fire_all, hourly};
use crate::scheduler::{AdminsOnly, allow_call_0, suspended_past_the_history};

const DAY: Duration = Duration::from_secs(24 * 3600);
const OTHER: &str = "p9-other";

/// Both windows at one day; every other setting as it is by default.
fn one_day() -> Retention {
    Retention {
        terminal_runs: Some(DAY),
        idle_sessions: Some(DAY),
        ..Retention::default()
    }
}

/// A run row and `events` event rows; `ended_days` is how long ago it
/// ended (NULL for none).
async fn run(
    db: &TestDb,
    namespace: &str,
    run_id: &str,
    status: &str,
    ended_days: Option<i64>,
    events: i64,
) {
    sqlx::query(
        "INSERT INTO linch_runs (namespace, run_id, session_id, contract, status, epoch, \
         checkpoint, last_seq, ended_at) \
         VALUES ($1, $2, $3, 'c', $4, 1, '{}', $5, now() - $6 * interval '1 day')",
    )
    .bind(namespace)
    .bind(run_id)
    .bind(format!("s-{run_id}"))
    .bind(status)
    .bind(events)
    .bind(ended_days.map(|days| days as f64))
    .execute(&db.pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO linch_run_events (namespace, run_id, seq, envelope) \
         SELECT $1, $2, n, 'e' FROM generate_series(1, $3::bigint) AS n",
    )
    .bind(namespace)
    .bind(run_id)
    .bind(events)
    .execute(&db.pool)
    .await
    .unwrap();
}

/// A session of `entries` log entries that was last written `idle_days` ago.
async fn session(db: &TestDb, namespace: &str, session_id: &str, idle_days: i64, entries: i64) {
    sqlx::query(
        "INSERT INTO linch_sessions (namespace, session_id, len, updated_at) \
         VALUES ($1, $2, $3, now() - $4 * interval '1 day')",
    )
    .bind(namespace)
    .bind(session_id)
    .bind(entries)
    .bind(idle_days as f64)
    .execute(&db.pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO linch_session_entries (namespace, session_id, position, entry) \
         SELECT $1, $2, n, 'x' FROM generate_series(0, $3::bigint - 1) AS n",
    )
    .bind(namespace)
    .bind(session_id)
    .bind(entries)
    .execute(&db.pool)
    .await
    .unwrap();
}

async fn count(db: &TestDb, table: &'static str, namespace: &str, id: &str) -> i64 {
    let column = match table {
        "linch_runs" | "linch_pruned_runs" => "run_id",
        "linch_run_events" => "run_id",
        _ => "session_id",
    };
    let sql = format!("SELECT count(*) FROM {table} WHERE namespace = $1 AND {column} = $2");
    sqlx::query_scalar(sqlx::AssertSqlSafe(sql))
        .bind(namespace)
        .bind(id)
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

/// The run row, its events and its tombstone: how many of each.
async fn run_rows(db: &TestDb, namespace: &str, id: &str) -> (i64, i64, i64) {
    (
        count(db, "linch_runs", namespace, id).await,
        count(db, "linch_run_events", namespace, id).await,
        count(db, "linch_pruned_runs", namespace, id).await,
    )
}

#[tokio::test]
async fn terminal_runs_past_the_window_go_with_their_events() {
    let Some(db) = TestDb::new("ret_runs").await else {
        return;
    };
    for (id, status) in [("c", "completed"), ("f", "failed"), ("a", "aborted")] {
        run(&db, NS, &format!("old-{id}"), status, Some(2), 8).await;
    }
    run(&db, NS, "young", "completed", Some(0), 8).await;
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.runs, done.events), (3, 24));
    for id in ["old-c", "old-f", "old-a"] {
        assert_eq!(run_rows(&db, NS, id).await, (0, 0, 1), "{id}");
    }
    assert_eq!(run_rows(&db, NS, "young").await, (1, 8, 0));
    // A second pass finds nothing.
    assert_eq!(prune_retained(&db.pool, &one_day()).await.unwrap().runs, 0);
    db.finish().await;
}

/// A run that was terminal before the retention migration has no end time:
/// the first pass with a window for runs gives it one (so its window starts
/// there) and deletes nothing of it; once it is old by that stamp, a pass does.
#[tokio::test]
async fn runs_that_ended_before_the_migration_start_their_window_at_the_first_pass() {
    let Some(db) = TestDb::new("ret_legacy").await else {
        return;
    };
    run(&db, NS, "legacy", "completed", None, 4).await;
    let no_runs = Retention {
        idle_sessions: Some(DAY),
        ..Retention::default()
    };
    assert_eq!(prune_retained(&db.pool, &no_runs).await.unwrap().stamped, 0);
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.stamped, done.runs), (1, 0));
    assert_eq!(run_rows(&db, NS, "legacy").await, (1, 4, 0));
    db.sql("UPDATE linch_runs SET ended_at = now() - interval '2 days'")
        .await;
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.stamped, done.runs, done.events), (0, 1, 4));
    db.finish().await;
}

#[tokio::test]
async fn a_pass_deletes_in_small_batches_until_none_is_left() {
    let Some(db) = TestDb::new("ret_batches").await else {
        return;
    };
    for i in 0..25 {
        run(&db, NS, &format!("r{i:02}"), "completed", Some(3), 2).await;
    }
    let mut retention = one_day();
    retention.batch_size = 4;
    let done = prune_retained(&db.pool, &retention).await.unwrap();
    assert_eq!((done.runs, done.events), (25, 50));
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_runs")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(left, 0);
    db.finish().await;
}

/// A window never reaches across a tenant: it deletes by (namespace, id), so
/// the same run id of another namespace, however old, is its own row; a
/// session likewise.
#[tokio::test]
async fn a_pass_deletes_by_namespace_and_id() {
    let Some(db) = TestDb::new("ret_tenant").await else {
        return;
    };
    run(&db, NS, "same", "completed", Some(5), 3).await;
    run(&db, OTHER, "same", "completed", Some(0), 3).await;
    session(&db, NS, "same-s", 5, 2).await;
    session(&db, OTHER, "same-s", 0, 2).await;
    prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(run_rows(&db, NS, "same").await, (0, 0, 1));
    assert_eq!(run_rows(&db, OTHER, "same").await, (1, 3, 0));
    assert_eq!(count(&db, "linch_sessions", NS, "same-s").await, 0);
    assert_eq!(count(&db, "linch_sessions", OTHER, "same-s").await, 1);
    assert_eq!(
        count(&db, "linch_session_entries", OTHER, "same-s").await,
        2
    );
    db.finish().await;
}

#[tokio::test]
async fn a_run_that_is_not_terminal_is_never_deleted() {
    let Some(db) = TestDb::new("ret_live").await else {
        return;
    };
    // Even with an end time long past (which no live run has), the status
    // keeps it.
    for status in ["pending", "running", "awaiting_approval", "awaiting_input"] {
        run(&db, NS, status, status, Some(400), 4).await;
        session(&db, NS, &format!("s-{status}"), 400, 2).await;
    }
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.runs, done.sessions), (0, 0));
    for status in ["pending", "running", "awaiting_approval", "awaiting_input"] {
        assert_eq!(run_rows(&db, NS, status).await, (1, 4, 0), "{status}");
        assert_eq!(
            count(&db, "linch_sessions", NS, &format!("s-{status}")).await,
            1
        );
    }
    db.finish().await;
}

/// A parent run reads its children's events when it resumes, so a child
/// stays while its parent is not terminal and goes with the next pass after.
#[tokio::test]
async fn a_child_run_stays_while_its_parent_is_live() {
    let Some(db) = TestDb::new("ret_child").await else {
        return;
    };
    run(&db, NS, "parent", "awaiting_approval", None, 3).await;
    run(&db, NS, "parent:call_1", "completed", Some(9), 3).await;
    sqlx::query("UPDATE linch_runs SET parent_run_id = 'parent' WHERE run_id = 'parent:call_1'")
        .execute(&db.pool)
        .await
        .unwrap();
    prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(run_rows(&db, NS, "parent:call_1").await, (1, 3, 0));
    sqlx::query(
        "UPDATE linch_runs SET status = 'completed', ended_at = now() - interval '9 days' \
         WHERE run_id = 'parent'",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(run_rows(&db, NS, "parent:call_1").await, (0, 0, 1));
    assert_eq!(run_rows(&db, NS, "parent").await, (0, 0, 1));
    db.finish().await;
}

/// A workflow's agent call (`wf-` run, session of the same id) is read back
/// by a resumed workflow whose journal has no record of it yet.
#[tokio::test]
async fn a_workflow_agent_run_and_its_session_stay() {
    let Some(db) = TestDb::new("ret_workflow").await else {
        return;
    };
    run(&db, NS, "wf-abc", "completed", Some(30), 5).await;
    session(&db, NS, "wf-abc", 30, 2).await;
    prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(run_rows(&db, NS, "wf-abc").await, (1, 5, 0));
    assert_eq!(count(&db, "linch_sessions", NS, "wf-abc").await, 1);
    db.finish().await;
}

/// A queued start job that recorded its run id starts another run when the
/// run it names is not found, so a run a job still names stays.
#[tokio::test]
async fn a_run_a_live_job_names_stays() {
    let Some(db) = TestDb::new("ret_job").await else {
        return;
    };
    run(&db, NS, "named", "completed", Some(9), 2).await;
    run(&db, NS, "failed-job", "completed", Some(9), 2).await;
    for (run_id, failed) in [("named", "NULL"), ("failed-job", "now()")] {
        let sql = format!(
            "INSERT INTO linch_worker_jobs (queue, namespace, session_id, prompt, run_id, failed_at) \
             VALUES ('default', '{NS}', 's', 'go', '{run_id}', {failed})"
        );
        sqlx::query(sqlx::AssertSqlSafe(sql))
            .execute(&db.pool)
            .await
            .unwrap();
    }
    prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(run_rows(&db, NS, "named").await, (1, 2, 0));
    assert_eq!(
        run_rows(&db, NS, "failed-job").await,
        (0, 0, 1),
        "a dead letter holds nothing"
    );
    db.finish().await;
}

#[tokio::test]
async fn idle_sessions_go_with_their_log_tasks_and_edges() {
    let Some(db) = TestDb::new("ret_sessions").await else {
        return;
    };
    session(&db, NS, "idle", 3, 4).await;
    session(&db, NS, "busy", 0, 4).await;
    for (table, columns, values) in [
        (
            "linch_tasks",
            "task_id, subject, description, status, metadata, created_at_ms, updated_at_ms",
            "1, 's', 'd', 'pending', '{}', 0, 0",
        ),
        ("linch_task_edges", "blocker, blocked", "'1', '2'"),
    ] {
        for id in ["idle", "busy"] {
            let sql = format!(
                "INSERT INTO {table} (namespace, session_id, {columns}) VALUES ('{NS}', '{id}', {values})"
            );
            sqlx::query(sqlx::AssertSqlSafe(sql))
                .execute(&db.pool)
                .await
                .unwrap();
        }
    }
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.sessions, done.entries), (1, 4));
    for table in ["linch_session_entries", "linch_tasks", "linch_task_edges"] {
        let sql = format!("SELECT session_id FROM {table} ORDER BY 1");
        let left: Vec<String> = sqlx::query_scalar(sqlx::AssertSqlSafe(sql))
            .fetch_all(&db.pool)
            .await
            .unwrap();
        assert!(
            left.iter().all(|id| id == "busy") && !left.is_empty(),
            "{table}: {left:?}"
        );
    }
    db.finish().await;
}

/// A session that has a run that is not terminal is not idle, however long
/// ago it was written; so is one a person may still continue after the run
/// ended: runs and sessions are separate windows.
#[tokio::test]
async fn a_session_with_a_live_run_stays_and_runs_and_sessions_are_separate_windows() {
    let Some(db) = TestDb::new("ret_windows").await else {
        return;
    };
    session(&db, NS, "s-held", 90, 2).await;
    run(&db, NS, "held", "awaiting_input", None, 2).await;
    sqlx::query("UPDATE linch_runs SET session_id = 's-held' WHERE run_id = 'held'")
        .execute(&db.pool)
        .await
        .unwrap();
    session(&db, NS, "s-done", 90, 2).await;
    run(&db, NS, "done", "completed", Some(90), 2).await;
    sqlx::query("UPDATE linch_runs SET session_id = 's-done' WHERE run_id = 'done'")
        .execute(&db.pool)
        .await
        .unwrap();
    // Only runs: the session of an ended run stays for a person to continue.
    let only_runs = Retention {
        terminal_runs: Some(DAY),
        ..Retention::default()
    };
    prune_retained(&db.pool, &only_runs).await.unwrap();
    assert_eq!(run_rows(&db, NS, "done").await, (0, 0, 1));
    assert_eq!(count(&db, "linch_sessions", NS, "s-done").await, 1);
    // Only sessions: the idle one goes, the held one stays.
    let only_sessions = Retention {
        idle_sessions: Some(DAY),
        ..Retention::default()
    };
    prune_retained(&db.pool, &only_sessions).await.unwrap();
    assert_eq!(count(&db, "linch_sessions", NS, "s-done").await, 0);
    assert_eq!(count(&db, "linch_sessions", NS, "s-held").await, 1);
    assert_eq!(run_rows(&db, NS, "held").await, (1, 2, 0));
    db.finish().await;
}

/// An append moves the session's clock, so a session in use is not idle.
#[tokio::test]
async fn an_append_keeps_a_session_from_going_idle() {
    let Some(db) = TestDb::new("ret_append").await else {
        return;
    };
    let sessions = PgSessionStore::new(db.pool.clone());
    let id = SessionId::new("talking");
    sessions.create(NS, &id).await.unwrap();
    sqlx::query("UPDATE linch_sessions SET updated_at = now() - interval '9 days'")
        .execute(&db.pool)
        .await
        .unwrap();
    let entry = LogEntry::Message(MessageEntry {
        message: Message::user_text("hi").into(),
        visible: true,
        historical: true,
    });
    sessions.append(NS, &id, 0, vec![entry]).await.unwrap();
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(done.sessions, 0);
    assert_eq!(sessions.load(NS, &id).await.unwrap().len(), 1);
    db.finish().await;
}

/// A run that ends is stamped by its commit, and the pass reads that stamp.
#[tokio::test]
async fn the_commit_that_ends_a_run_stamps_its_end() {
    let Some(db) = TestDb::new("ret_stamp").await else {
        return;
    };
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe));
    enqueue_run(&db.pool, "default", NS, &SessionId::new("talk"), "go")
        .await
        .unwrap();
    let run_id = db.run_of("talk").await;
    wait_until("the run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    let ended: Option<bool> = sqlx::query_scalar(
        "SELECT ended_at BETWEEN now() - interval '1 minute' AND now() FROM linch_runs \
         WHERE run_id = $1",
    )
    .bind(run_id.as_str())
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(ended, Some(true));
    worker.stop().await;
    db.finish().await;
}

#[tokio::test]
async fn a_deleted_run_reads_back_as_pruned_until_its_tombstone_expires() {
    let Some(db) = TestDb::new("ret_pruned").await else {
        return;
    };
    run(&db, NS, "gone", "completed", Some(5), 3).await;
    let runs = PgRunStore::new(db.pool.clone());
    let id = RunId::new("gone");
    assert_eq!(run_rows(&db, NS, "gone").await, (1, 3, 0));
    prune_retained(&db.pool, &one_day()).await.unwrap();
    let pruned = |error: StoreError| matches!(error, StoreError::NotFound { pruned: true, .. });
    assert!(pruned(runs.load(NS, &id).await.unwrap_err()));
    assert!(pruned(runs.events(NS, &id, 0).await.unwrap_err()));
    assert!(pruned(runs.claim(NS, &id, "w").await.unwrap_err()));
    // A wrong id, and the same id in another namespace, are plain not found.
    let never = RunId::new("never");
    let missing = runs.load(NS, &never).await.unwrap_err();
    assert!(
        matches!(missing, StoreError::NotFound { pruned: false, .. }),
        "{missing:?}"
    );
    let other = runs.load(OTHER, &id).await.unwrap_err();
    assert!(
        matches!(other, StoreError::NotFound { pruned: false, .. }),
        "{other:?}"
    );
    // Past its tombstone period the run is simply not found.
    sqlx::query("UPDATE linch_pruned_runs SET pruned_at = now() - interval '40 days'")
        .execute(&db.pool)
        .await
        .unwrap();
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(done.tombstones, 1);
    let error = runs.load(NS, &id).await.unwrap_err();
    assert!(
        matches!(error, StoreError::NotFound { pruned: false, .. }),
        "{error:?}"
    );
    db.finish().await;
}

/// Another pruner holds the pruner lock: the pass deletes nothing and says so.
#[tokio::test]
async fn one_pruner_runs_at_a_time() {
    let Some(db) = TestDb::new("ret_lock").await else {
        return;
    };
    run(&db, NS, "old", "completed", Some(5), 2).await;
    let mut other = db.pool.acquire().await.unwrap();
    let held: bool = sqlx::query_scalar(
        "SELECT pg_try_advisory_lock(hashtextextended('linch_retention:' || current_database() \
         || ':' || coalesce(current_schema(), ''), 0))",
    )
    .fetch_one(&mut *other)
    .await
    .unwrap();
    assert!(held);
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert!(done.contended);
    assert_eq!((done.runs, done.sessions), (0, 0));
    assert_eq!(run_rows(&db, NS, "old").await, (1, 2, 0));
    drop(other);
    db.finish().await;
}

/// A statement of a batch that fails rolls the whole batch back: every run is
/// whole or gone with its tombstone, whichever write dies.
#[tokio::test]
async fn a_batch_that_fails_at_any_write_changes_nothing() {
    let Some(db) = TestDb::new("ret_crash").await else {
        return;
    };
    run(&db, NS, "r1", "completed", Some(5), 3).await;
    run(&db, NS, "r2", "completed", Some(5), 3).await;
    session(&db, NS, "idle", 5, 3).await;
    let only_runs = Retention {
        terminal_runs: Some(DAY),
        ..Retention::default()
    };
    let only_sessions = Retention {
        idle_sessions: Some(DAY),
        ..Retention::default()
    };
    for (table, event, retention) in [
        ("linch_runs", "DELETE", &only_runs),
        ("linch_run_events", "DELETE", &only_runs),
        ("linch_pruned_runs", "INSERT", &only_runs),
        ("linch_sessions", "DELETE", &only_sessions),
        ("linch_session_entries", "DELETE", &only_sessions),
    ] {
        let create = format!(
            "CREATE FUNCTION boom() RETURNS trigger LANGUAGE plpgsql AS \
             $$ BEGIN RAISE EXCEPTION 'boom'; END $$; \
             CREATE TRIGGER boom BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION boom()"
        );
        sqlx::raw_sql(sqlx::AssertSqlSafe(create))
            .execute(&db.pool)
            .await
            .unwrap();
        let outcome = prune_retained(&db.pool, retention).await;
        sqlx::raw_sql(sqlx::AssertSqlSafe(format!(
            "DROP TRIGGER boom ON {table}; DROP FUNCTION boom()"
        )))
        .execute(&db.pool)
        .await
        .unwrap();
        assert!(
            outcome.is_err(),
            "{table}: the failed write is the pass's error"
        );
        for id in ["r1", "r2"] {
            assert_eq!(run_rows(&db, NS, id).await, (1, 3, 0), "{table} {id}");
        }
        assert_eq!(count(&db, "linch_sessions", NS, "idle").await, 1, "{table}");
        assert_eq!(
            count(&db, "linch_session_entries", NS, "idle").await,
            3,
            "{table}"
        );
    }
    // With the fault gone the same pass deletes everything.
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!(
        (done.runs, done.events, done.sessions, done.entries),
        (2, 6, 1, 3)
    );
    db.finish().await;
}

/// The #165 defect, pinned for retention: a run suspended past every window
/// survives a pass, and an approve after it still runs the run.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_run_suspended_past_the_windows_survives_and_can_be_approved() {
    let Some(db) = TestDb::new("ret_approve").await else {
        return;
    };
    let run_id = suspended_past_the_history(&db).await;
    // Everything about it is old: the session, and (as no live run has) an end time.
    sqlx::query("UPDATE linch_sessions SET updated_at = now() - interval '400 days'")
        .execute(&db.pool)
        .await
        .unwrap();
    sqlx::query("UPDATE linch_runs SET ended_at = now() - interval '400 days'")
        .execute(&db.pool)
        .await
        .unwrap();
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.runs, done.sessions), (0, 0));
    assert_eq!(db.status(&run_id).await, RunStatus::AwaitingApproval);
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    enqueue_approve(&db.pool, "default", NS, &run_id, allow_call_0())
        .await
        .unwrap();
    let shutdown = CancellationToken::new();
    let factory = AdminsOnly {
        pool: db.pool.clone(),
        probe: probe.clone(),
    };
    let worker =
        Worker::new(db.pool.clone(), config("w2")).definitions([NS.to_owned()], Arc::new(factory));
    let handle = tokio::spawn(worker.run(shutdown.clone()));
    wait_until("the approved run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    assert_eq!(probe.started(), 1);
    shutdown.cancel();
    handle.await.unwrap().unwrap();
    db.finish().await;
}

/// Pruning the runs of a schedule neither re-fires it nor double-runs a
/// job, and the job list reads what it did.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pruning_the_runs_of_a_schedule_leaves_its_fires_and_jobs_alone() {
    let Some(db) = TestDb::new("ret_schedule").await else {
        return;
    };
    linch_worker::create_schedule(&db.pool, &hourly("h"), None)
        .await
        .unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    let run_id = db.run_of("sched").await;
    wait_until(
        "the scheduled run completes and its job is acked",
        || async { db.status(&run_id).await == RunStatus::Completed && db.jobs().await == 0 },
    )
    .await;
    let before = jobs(&db.pool, NS, Some("h"), 10).await.unwrap();
    let next = get_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert_eq!((before.len(), next.fires), (1, 1));

    // The run is two days old now, the fire's session idle for as long.
    db.sql("UPDATE linch_runs SET ended_at = now() - interval '2 days'")
        .await;
    db.sql("UPDATE linch_sessions SET updated_at = now() - interval '2 days'")
        .await;
    let done = prune_retained(&db.pool, &one_day()).await.unwrap();
    assert_eq!((done.runs, done.sessions), (1, 1));

    assert_eq!(jobs(&db.pool, NS, Some("h"), 10).await.unwrap(), before);
    let kept = job(&db.pool, NS, before[0].id).await.unwrap();
    assert_eq!(kept.as_ref(), before.first());
    let after = get_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert_eq!(
        (after.next_fire, after.fires, after.skips),
        (next.next_fire, 1, 0),
        "a pass moves nothing of the schedule"
    );
    // Nothing is due until the next hour; then the fire comes once.
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    assert_eq!(fire_all(&db).await, 1);
    wait_until("the second fire runs once", || async {
        probe.started() == 2 && db.jobs().await == 0
    })
    .await;
    assert_eq!(
        get_schedule(&db.pool, NS, "h")
            .await
            .unwrap()
            .unwrap()
            .fires,
        2
    );
    assert_eq!(jobs(&db.pool, NS, Some("h"), 10).await.unwrap().len(), 2);
    worker.stop().await;
    db.finish().await;
}
