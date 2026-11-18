//! Heartbeat schedules (one fixed session), jobs with a stored agent
//! definition run by a factory, job status and history, and a fire that
//! crashes before its commit.

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

use futures::FutureExt;
use futures::future::BoxFuture;
use linch_core::hooks::ApprovalResponse;
use linch_core::permissions::{Rule, RuleDecision, ToolRule};
use linch_core::{PermissionLayer, Principal, RunId, RunOptions, RunStatus, SessionId, ToolScope};
use linch_postgres::PgPool;
use linch_worker::{
    AgentFactory, Cap, FactoryError, JobAgent, JobId, JobState, RunRequest, Timing, Worker,
    WorkerError, create_schedule, delete_schedule, enqueue, enqueue_approve, enqueue_resume,
    enqueue_run, fire_due_schedules, get_schedule, job, jobs, prune_definitions, prune_history,
};
use tokio_util::sync::CancellationToken;

use crate::common::*;
use crate::schedule::{fire_all, hourly};

async fn make_due(db: &TestDb, ago: &'static str) {
    let sql = format!("UPDATE linch_worker_schedules SET next_fire = now() - interval '{ago}'");
    sqlx::query(sqlx::AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();
}

#[tokio::test]
async fn a_heartbeat_skips_a_fire_while_its_last_one_is_unfinished() {
    let Some(db) = TestDb::new("beat_job").await else {
        return;
    };
    // An interval: an hourly cron due a second ago has its next tick in the
    // past when the test runs in the first second of an hour.
    let mut beat = hourly("beat");
    beat.timing = Timing::Every(Duration::from_secs(3600));
    create_schedule(&db.pool, &beat, None).await.unwrap();
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    // Its job is still queued: the next fire is skipped, not stacked.
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 1);
    let info = get_schedule(&db.pool, NS, "beat").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (1, 1));
    assert!(info.next_fire > crate::schedule::now(&db).await);
    db.finish().await;
}

#[tokio::test]
async fn a_heartbeats_missed_fires_collapse_into_one() {
    let Some(db) = TestDb::new("beat_catchup").await else {
        return;
    };
    // An interval, not an hourly cron: how many cron ticks fit in the last
    // 150 minutes depends on the minute the test runs at.
    let mut beat = hourly("beat");
    beat.timing = Timing::Every(Duration::from_secs(3600));
    create_schedule(&db.pool, &beat, None).await.unwrap();
    // Due 150, 90 and 30 minutes ago: the first fires, the other two skip.
    make_due(&db, "150 minutes").await;
    assert_eq!(fire_all(&db).await, 1);
    let info = get_schedule(&db.pool, NS, "beat").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (1, 2));
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_heartbeat_skips_a_fire_while_its_session_has_a_live_run() {
    let Some(db) = TestDb::new("beat_live").await else {
        return;
    };
    // A run in the session that suspends for approval: its job ends, but
    // the run stays live until a person answers.
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe));
    enqueue_run(&db.pool, "default", NS, &SessionId::new("sched"), "go")
        .await
        .unwrap();
    let run_id = db.run_of("sched").await;
    wait_until("the run suspends and its job ends", || async {
        db.status(&run_id).await == RunStatus::AwaitingApproval && db.jobs().await == 0
    })
    .await;
    worker.stop().await;
    create_schedule(&db.pool, &hourly("beat"), None)
        .await
        .unwrap();
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 0);
    let info = get_schedule(&db.pool, NS, "beat").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (0, 1));
    // Still suspended at the next tick: still skipped.
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);

    // Cancelled through the store, the run is terminal: the next fire runs.
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let canceller = agent(&db.pool, probe.clone());
    let options = RunOptions::new(SessionId::new("sched"));
    assert!(canceller.cancel(run_id.clone(), options).await.unwrap());
    assert_eq!(db.status(&run_id).await, RunStatus::Aborted);
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    let worker = Running::spawn(&db.pool, config("w2"), agent(&db.pool, probe));
    let runs = || async {
        let runs: i64 =
            sqlx::query_scalar("SELECT count(*) FROM linch_runs WHERE session_id = 'sched'")
                .fetch_one(&db.pool)
                .await
                .unwrap();
        runs
    };
    wait_until("the fire's run starts and its job ends", || async {
        runs().await == 2 && db.jobs().await == 0
    })
    .await;
    worker.stop().await;
    let info = get_schedule(&db.pool, NS, "beat").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (1, 2));
    db.finish().await;
}

/// Opens the probe agent for any definition but `refuse`, counting opens
/// and closes.
struct Factory {
    pool: PgPool,
    probe: Probe,
    opened: Arc<AtomicU32>,
    closed: Arc<AtomicU32>,
}

impl AgentFactory for Factory {
    fn open<'a>(
        &'a self,
        namespace: &'a str,
        definition: &'a str,
    ) -> BoxFuture<'a, Result<JobAgent, FactoryError>> {
        async move {
            assert_eq!(namespace, NS);
            if definition == "refuse" {
                return Err(FactoryError::Refused("not allowed".into()));
            }
            self.opened.fetch_add(1, Ordering::SeqCst);
            let closed = Arc::clone(&self.closed);
            let close = async move {
                closed.fetch_add(1, Ordering::SeqCst);
            };
            Ok(JobAgent::new(agent(&self.pool, self.probe.clone())).on_close(close.boxed()))
        }
        .boxed()
    }
}

fn request(session: &str, definition: &str) -> RunRequest {
    RunRequest {
        queue: "default".into(),
        namespace: NS.into(),
        session_id: SessionId::new(session),
        prompt: "go".into(),
        agent: Some(definition.into()),
        principal: None,
    }
}

async fn state(db: &TestDb, id: JobId) -> JobState {
    job(&db.pool, NS, id).await.unwrap().unwrap().state
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_factory_runs_jobs_with_a_stored_definition() {
    let Some(db) = TestDb::new("factory").await else {
        return;
    };
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    // A worker with only a fixed agent leaves a definition job alone.
    let fixed = Running::spawn(&db.pool, config("w0"), agent(&db.pool, probe.clone()));
    let first = enqueue(&db.pool, &request("s1", "def-1"), None)
        .await
        .unwrap();
    assert_eq!(state(&db, first).await, JobState::Queued);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(state(&db, first).await, JobState::Queued);
    fixed.stop().await;

    let (opened, closed) = (Arc::default(), Arc::<AtomicU32>::default());
    let factory = Factory {
        pool: db.pool.clone(),
        probe: probe.clone(),
        opened: Arc::clone(&opened),
        closed: Arc::clone(&closed),
    };
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(db.pool.clone(), config("w1")).definitions([NS.to_owned()], Arc::new(factory));
    let handle = tokio::spawn(worker.run(shutdown.clone()));
    let refused = enqueue(&db.pool, &request("s2", "refuse"), None)
        .await
        .unwrap();
    wait_until("both jobs end", || async {
        state(&db, first).await == JobState::Done && state(&db, refused).await == JobState::Failed
    })
    .await;
    let done = job(&db.pool, NS, first).await.unwrap().unwrap();
    let run_id = done.run_id.clone().expect("the job names its run");
    assert_eq!(db.status(&run_id).await, RunStatus::Completed);
    assert_eq!(done.session_id, Some(SessionId::new("s1")));
    assert!(done.finished_at.is_some());
    let failed = job(&db.pool, NS, refused).await.unwrap().unwrap();
    assert!(failed.error.unwrap().contains("not allowed"));
    assert_eq!(
        (opened.load(Ordering::SeqCst), closed.load(Ordering::SeqCst)),
        (1, 1)
    );
    // The same definition is stored once.
    let stored: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_worker_agents")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(stored, 2);
    // Listed newest first; another namespace sees none of them.
    let listed: Vec<JobId> = jobs(&db.pool, NS, None, 10)
        .await
        .unwrap()
        .into_iter()
        .map(|job| job.id)
        .collect();
    assert_eq!(listed, vec![refused, first]);
    assert!(job(&db.pool, "other", first).await.unwrap().is_none());
    assert!(jobs(&db.pool, "other", None, 10).await.unwrap().is_empty());
    shutdown.cancel();
    handle.await.unwrap().unwrap();
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn scheduled_jobs_are_listed_by_schedule() {
    let Some(db) = TestDb::new("by_schedule").await else {
        return;
    };
    let mut every = hourly("h");
    every.session_id = None;
    every.timing = Timing::Every(Duration::from_secs(3600));
    create_schedule(&db.pool, &every, None).await.unwrap();
    make_due(&db, "1 second").await;
    assert_eq!(fire_all(&db).await, 1);
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s"), "go")
        .await
        .unwrap();
    let all = jobs(&db.pool, NS, None, 10).await.unwrap();
    let by_schedule = jobs(&db.pool, NS, Some("h"), 10).await.unwrap();
    assert_eq!((all.len(), by_schedule.len()), (2, 1));
    assert!(by_schedule.iter().all(|job| job.fire_at.is_some()));
    assert_eq!(jobs(&db.pool, NS, None, 1).await.unwrap().len(), 1);
    db.finish().await;
}

#[tokio::test]
async fn the_job_cap_counts_unfinished_jobs() {
    let Some(db) = TestDb::new("job_cap").await else {
        return;
    };
    let cap = Cap {
        root: NS.into(),
        max: 1,
    };
    enqueue(&db.pool, &request("s1", "d"), Some(&cap))
        .await
        .unwrap();
    let refused = enqueue(&db.pool, &request("s2", "d"), Some(&cap)).await;
    assert!(matches!(
        refused,
        Err(WorkerError::LimitReached {
            what: "jobs",
            max: 1
        })
    ));
    // A failed job no longer counts.
    db.sql("UPDATE linch_worker_jobs SET failed_at = now()")
        .await;
    enqueue(&db.pool, &request("s3", "d"), Some(&cap))
        .await
        .unwrap();
    db.finish().await;
}

/// Pruning the history also deletes failed jobs that failed longer ago than
/// the retention: a dead letter is readable for as long as an ended job.
#[tokio::test]
async fn failed_jobs_are_pruned_with_the_history() {
    let Some(db) = TestDb::new("prune_failed").await else {
        return;
    };
    for session in ["old", "new", "live"] {
        enqueue_run(&db.pool, "default", NS, &SessionId::new(session), "go")
            .await
            .unwrap();
    }
    db.sql(
        "UPDATE linch_worker_jobs SET failed_at = now() - interval '8 days', error = 'boom' \
         WHERE session_id = 'old'",
    )
    .await;
    db.sql(
        "UPDATE linch_worker_jobs SET failed_at = now() - interval '1 day', error = 'boom' \
         WHERE session_id = 'new'",
    )
    .await;
    let week = Duration::from_secs(7 * 24 * 3600);
    assert_eq!(prune_history(&db.pool, week).await.unwrap(), 1);
    let left: Vec<String> =
        sqlx::query_scalar("SELECT session_id FROM linch_worker_jobs ORDER BY session_id")
            .fetch_all(&db.pool)
            .await
            .unwrap();
    assert_eq!(left, ["live", "new"]);
    db.finish().await;
}

/// Any schedule, with a fixed session or not, skips a fire while its last
/// fire's job is unfinished: a sessionless schedule no longer stacks a job
/// per fire behind a backlog.
#[tokio::test]
async fn a_sessionless_schedule_skips_a_fire_while_its_last_job_is_unfinished() {
    let Some(db) = TestDb::new("fire_unfinished").await else {
        return;
    };
    let mut every = hourly("every");
    every.session_id = None;
    every.timing = Timing::Every(Duration::from_secs(3600));
    create_schedule(&db.pool, &every, None).await.unwrap();
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 1);
    let info = get_schedule(&db.pool, NS, "every").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (1, 1));
    // Once that job failed for good, the next fire is made.
    db.sql("UPDATE linch_worker_jobs SET failed_at = now()")
        .await;
    make_due(&db, "1 second").await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    db.finish().await;
}

/// A fire meets the tenant's `max_queued_jobs` as `enqueue` does, but is
/// skipped (counted in `skips`, its next fire moved on), not failed.
#[tokio::test]
async fn a_fire_at_the_job_cap_is_skipped() {
    let Some(db) = TestDb::new("fire_cap").await else {
        return;
    };
    let cap = Cap {
        root: NS.into(),
        max: 2,
    };
    // One unfinished job already: room for one fire of the two.
    enqueue(&db.pool, &request("s1", "d"), Some(&cap))
        .await
        .unwrap();
    for id in ["a", "b"] {
        let mut every = hourly(id);
        every.session_id = None;
        every.timing = Timing::Every(Duration::from_secs(3600));
        create_schedule(&db.pool, &every, None).await.unwrap();
    }
    make_due(&db, "1 second").await;
    let before = crate::schedule::now(&db).await;
    assert_eq!(
        fire_due_schedules(&db.pool, std::slice::from_ref(&cap))
            .await
            .unwrap(),
        1
    );
    assert_eq!(db.jobs().await, 2);
    let mut counts = Vec::new();
    for id in ["a", "b"] {
        let info = get_schedule(&db.pool, NS, id).await.unwrap().unwrap();
        assert!(info.next_fire > before, "{id} moved on");
        counts.push((info.fires, info.skips));
    }
    counts.sort_unstable();
    assert_eq!(counts, [(0, 1), (1, 0)]);
    // A cap of another root does not hold here.
    let other = Cap {
        root: "elsewhere".into(),
        max: 0,
    };
    make_due(&db, "1 second").await;
    db.sql("UPDATE linch_worker_jobs SET failed_at = now() WHERE schedule_id IS NOT NULL")
        .await;
    assert_eq!(fire_due_schedules(&db.pool, &[other]).await.unwrap(), 2);
    db.finish().await;
}

/// The test schema's trigger on job inserts, which runs `body`.
async fn job_insert_trigger(db: &TestDb, body: &str) {
    let sql = format!(
        "CREATE FUNCTION crash_fire() RETURNS trigger LANGUAGE plpgsql AS $$ \
         BEGIN {body} RETURN NEW; END $$; \
         CREATE TRIGGER crash_fire BEFORE INSERT ON linch_worker_jobs \
         FOR EACH ROW EXECUTE FUNCTION crash_fire();"
    );
    sqlx::raw_sql(sqlx::AssertSqlSafe(sql))
        .execute(&db.pool)
        .await
        .unwrap();
}

async fn drop_trigger(db: &TestDb) {
    db.sql("DROP TRIGGER crash_fire ON linch_worker_jobs").await;
}

#[tokio::test]
async fn a_fire_that_fails_before_its_commit_leaves_nothing() {
    let Some(db) = TestDb::new("fire_abort").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    make_due(&db, "1 second").await;
    let due = get_schedule(&db.pool, NS, "h")
        .await
        .unwrap()
        .unwrap()
        .next_fire;
    job_insert_trigger(&db, "RAISE EXCEPTION 'crash';").await;
    assert!(fire_due_schedules(&db.pool, &[]).await.is_err());
    assert_eq!(db.jobs().await, 0);
    let info = get_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert_eq!((info.next_fire, info.fires), (due, 0), "nothing moved");
    drop_trigger(&db).await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 1);

    // The dedupe key guards the same fire time a second time.
    sqlx::query("UPDATE linch_worker_schedules SET next_fire = $1, skips = 0")
        .bind(due)
        .execute(&db.pool)
        .await
        .unwrap();
    db.sql("UPDATE linch_worker_jobs SET schedule_id = NULL")
        .await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 1);
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_firer_killed_mid_transaction_leaves_nothing() {
    let Some(db) = TestDb::new("fire_kill").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    make_due(&db, "1 second").await;
    job_insert_trigger(&db, "PERFORM pg_sleep(30);").await;
    // The firer's connection is killed while it waits inside the insert,
    // after the schedule row was locked and before the commit.
    let firing = tokio::spawn({
        let pool = db.pool.clone();
        async move { fire_due_schedules(&pool, &[]).await }
    });
    wait_until("the firer sleeps in its insert", || async {
        let pid: Option<i32> = sqlx::query_scalar(
            "SELECT pid FROM pg_stat_activity \
             WHERE wait_event = 'PgSleep' AND query LIKE 'INSERT INTO linch_worker_jobs%'",
        )
        .fetch_optional(&db.pool)
        .await
        .unwrap();
        if let Some(pid) = pid {
            sqlx::query("SELECT pg_terminate_backend($1)")
                .bind(pid)
                .execute(&db.pool)
                .await
                .unwrap();
        }
        pid.is_some()
    })
    .await;
    assert!(firing.await.unwrap().is_err(), "the fire did not commit");
    drop_trigger(&db).await;
    assert_eq!(db.jobs().await, 0);
    let info = get_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert_eq!(info.fires, 0);
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    assert_eq!(db.jobs().await, 1);
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_firers_make_each_fire_once() {
    let Some(db) = TestDb::new("fire_race").await else {
        return;
    };
    for i in 0..20 {
        let mut every = hourly(&format!("s{i}"));
        every.session_id = None;
        every.timing = Timing::Every(Duration::from_secs(60));
        create_schedule(&db.pool, &every, None).await.unwrap();
    }
    make_due(&db, "150 seconds").await;
    let firers = (0..8).map(|_| {
        let pool = db.pool.clone();
        tokio::spawn(async move {
            let mut total = 0;
            for _ in 0..10 {
                total += fire_due_schedules(&pool, &[]).await.unwrap();
            }
            total
        })
    });
    let mut fired = 0;
    for firer in firers {
        fired += firer.await.unwrap();
    }
    fired += fire_all(&db).await;
    // One fire each (the ticks 90 s and 30 s ago coalesce into it), none
    // twice.
    assert_eq!(fired, 20);
    assert_eq!(db.jobs().await, 20);
    let distinct: i64 =
        sqlx::query_scalar("SELECT count(DISTINCT (schedule_id, fire_at)) FROM linch_worker_jobs")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(distinct, 20);
    db.finish().await;
}

/// A stored definition holds config secrets, so pruning deletes it once no
/// schedule and no unfinished job names it; a failed job lets go of it.
#[tokio::test]
async fn a_definition_no_one_names_is_pruned() {
    let Some(db) = TestDb::new("prune_defs").await else {
        return;
    };
    let token = "tok-sentinel-5b1f";
    let held = |db: &TestDb| {
        let pool = db.pool.clone();
        async move {
            let held: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM linch_worker_agents WHERE strpos(config, $1) > 0",
            )
            .bind(token)
            .fetch_one(&pool)
            .await
            .unwrap();
            held
        }
    };
    let mut beat = hourly("beat");
    beat.timing = Timing::Every(Duration::from_secs(3600));
    beat.agent = Some(format!(
        r#"{{"mcp": {{"headers": {{"x-token": "{token}"}}}}}}"#
    ));
    create_schedule(&db.pool, &beat, None).await.unwrap();
    let mut kept = hourly("kept");
    kept.agent = Some(r#"{"kept": true}"#.into());
    create_schedule(&db.pool, &kept, None).await.unwrap();
    sqlx::query("UPDATE linch_worker_schedules SET next_fire = now() WHERE id = 'beat'")
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
    assert!(delete_schedule(&db.pool, NS, "beat").await.unwrap());

    // Its fire's job is unfinished: the definition stays.
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 0);
    assert_eq!(held(&db).await, 1);
    // The job fails for good: nothing can run it again.
    sqlx::query("UPDATE linch_worker_jobs SET failed_at = now(), error = 'boom'")
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 1);
    assert_eq!(held(&db).await, 0, "the token is gone");
    let failed: Option<String> = sqlx::query_scalar("SELECT agent FROM linch_worker_jobs")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(failed, None);
    // A schedule still names the other one.
    let left: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_worker_agents")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(left, 1);
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 0);

    // A running worker prunes on its own, from its first poll.
    assert!(delete_schedule(&db.pool, NS, "kept").await.unwrap());
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe));
    wait_until("the worker prunes the last definition", || async {
        let left: i64 = sqlx::query_scalar("SELECT count(*) FROM linch_worker_agents")
            .fetch_one(&db.pool)
            .await
            .unwrap();
        left == 0
    })
    .await;
    worker.stop().await;
    db.finish().await;
}

/// A queued run and a schedule's fire act for the principal they were made
/// for, so a rule that denies a tool to its role holds in the background:
/// without the principal the write tool would ask and the run suspend.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_background_run_acts_for_its_principal() {
    let Some(db) = TestDb::new("principal").await else {
        return;
    };
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let mut deny = ToolRule::new("Write", RuleDecision::Deny);
    deny.role = Some("intern".into());
    let layer = PermissionLayer::new("roles", [Rule::Tool(deny)]);
    let worker = Running::spawn(
        &db.pool,
        config("w1"),
        ruled_agent(&db.pool, probe.clone(), layer),
    );
    let intern = Principal::new("acme", "ada", ["intern"]).unwrap();
    let mut queued = request("queued", "unused");
    (queued.agent, queued.principal) = (None, Some(intern.clone()));
    enqueue(&db.pool, &queued, None).await.unwrap();
    let mut beat = hourly("beat");
    beat.session_id = Some(SessionId::new("fired"));
    beat.timing = Timing::Every(Duration::from_secs(3600));
    beat.principal = Some(intern);
    create_schedule(&db.pool, &beat, None).await.unwrap();
    // The running worker fires it.
    make_due(&db, "1 second").await;
    for session in ["queued", "fired"] {
        let run_id = db.run_of(session).await;
        wait_until("the run ends", || async {
            db.status(&run_id).await.is_terminal()
        })
        .await;
        assert_eq!(db.status(&run_id).await, RunStatus::Completed, "{session}");
    }
    assert_eq!(probe.started(), 0, "the rule denied the intern's write");
    worker.stop().await;
    db.finish().await;
}

/// Opens, for every definition, the probe agent under a rule that denies
/// `Write` to all but `admin`: an admin's write asks, anyone else's is
/// denied.
pub(crate) struct AdminsOnly {
    pub(crate) pool: PgPool,
    pub(crate) probe: Probe,
}

impl AgentFactory for AdminsOnly {
    fn open<'a>(
        &'a self,
        _namespace: &'a str,
        _definition: &'a str,
    ) -> BoxFuture<'a, Result<JobAgent, FactoryError>> {
        let mut deny = ToolRule::new("Write", RuleDecision::Deny);
        deny.unless_role = Some("admin".into());
        let layer = PermissionLayer::new("roles", [Rule::Tool(deny)]);
        let agent = ruled_agent(&self.pool, self.probe.clone(), layer);
        async move { Ok(JobAgent::new(agent)) }.boxed()
    }
}

/// A scheduled run that suspends for approval is continued through the
/// queue: `enqueue_approve` gives the job the run's stored definition, so a
/// worker with only a factory takes it, and the run goes on for the
/// principal it was started with.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_approve_job_continues_a_scheduled_run_with_its_agent_and_principal() {
    let Some(db) = TestDb::new("approve_fire").await else {
        return;
    };
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let factory = AdminsOnly {
        pool: db.pool.clone(),
        probe: probe.clone(),
    };
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(db.pool.clone(), config("w1")).definitions([NS.to_owned()], Arc::new(factory));
    let handle = tokio::spawn(worker.run(shutdown.clone()));
    let mut fired = hourly("fired");
    fired.session_id = None;
    fired.timing = Timing::Every(Duration::from_secs(3600));
    fired.agent = Some("def".into());
    fired.principal = Some(Principal::new("acme", "ada", ["admin"]).unwrap());
    create_schedule(&db.pool, &fired, None).await.unwrap();
    // The worker fires it.
    make_due(&db, "1 second").await;
    wait_until("the fire suspends and its job ends", || async {
        let listed = jobs(&db.pool, NS, Some("fired"), 10).await.unwrap();
        listed
            .first()
            .is_some_and(|job| job.state == JobState::Done)
    })
    .await;
    let listed = jobs(&db.pool, NS, Some("fired"), 10).await.unwrap();
    let run_id = listed[0].run_id.clone().expect("the fire names its run");
    assert_eq!(db.status(&run_id).await, RunStatus::AwaitingApproval);
    // The suspended run keeps its definition after its schedule is gone.
    assert!(delete_schedule(&db.pool, NS, "fired").await.unwrap());
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 0);

    let approvals = [("call_0".to_owned(), ApprovalResponse::Allow)];
    enqueue_approve(&db.pool, "default", NS, &run_id, approvals)
        .await
        .unwrap();
    wait_until("the approved run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    assert_eq!(probe.started(), 1, "the write ran for the admin");
    shutdown.cancel();
    handle.await.unwrap().unwrap();
    // Once the run ended, nothing keeps the definition.
    wait_until("the approve job is acked", || async {
        db.jobs().await == 0
    })
    .await;
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 1);
    db.finish().await;
}

/// A scheduled run that suspended for an approval, its fire's job acked, its
/// schedule deleted and the worker stopped; the history then pruned, as it is
/// after the retention (7 days by default) with nobody having answered.
pub(crate) async fn suspended_past_the_history(db: &TestDb) -> RunId {
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let factory = AdminsOnly {
        pool: db.pool.clone(),
        probe,
    };
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(db.pool.clone(), config("w1")).definitions([NS.to_owned()], Arc::new(factory));
    let handle = tokio::spawn(worker.run(shutdown.clone()));
    let mut fired = hourly("fired");
    fired.session_id = None;
    fired.timing = Timing::Every(Duration::from_secs(3600));
    fired.agent = Some("def".into());
    fired.principal = Some(Principal::new("acme", "ada", ["admin"]).unwrap());
    create_schedule(&db.pool, &fired, None).await.unwrap();
    make_due(db, "1 second").await;
    wait_until("the fire suspends and its job ends", || async {
        let listed = jobs(&db.pool, NS, Some("fired"), 10).await.unwrap();
        listed
            .first()
            .is_some_and(|job| job.state == JobState::Done)
    })
    .await;
    let listed = jobs(&db.pool, NS, Some("fired"), 10).await.unwrap();
    let run_id = listed[0].run_id.clone().expect("the fire names its run");
    assert_eq!(db.status(&run_id).await, RunStatus::AwaitingApproval);
    shutdown.cancel();
    handle.await.unwrap().unwrap();
    assert!(delete_schedule(&db.pool, NS, "fired").await.unwrap());
    prune_history(&db.pool, Duration::ZERO).await.unwrap();
    prune_definitions(&db.pool).await.unwrap();
    assert_eq!(db.status(&run_id).await, RunStatus::AwaitingApproval);
    run_id
}

pub(crate) fn allow_call_0() -> [(String, ApprovalResponse); 1] {
    [("call_0".to_owned(), ApprovalResponse::Allow)]
}

/// The approve job of a run that no row names any more must carry an agent,
/// or be refused: a job with no agent can never run on a factory-only worker.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_approve_after_the_history_was_pruned_still_has_an_agent() {
    let Some(db) = TestDb::new("approve_pruned").await else {
        return;
    };
    let run_id = suspended_past_the_history(&db).await;
    let queued = enqueue_approve(&db.pool, "default", NS, &run_id, allow_call_0()).await;
    let agent: Option<String> = sqlx::query_scalar("SELECT agent FROM linch_worker_jobs")
        .fetch_optional(&db.pool)
        .await
        .unwrap()
        .flatten();
    assert!(
        queued.is_err() || agent.is_some(),
        "the approve job of a suspended run has no agent, so no worker can run it"
    );
    db.finish().await;
}

/// The approve of a run past the history retention goes through: the history
/// row that names the run's definition is kept while the run is suspended, so
/// a worker with only a factory takes the job and the run goes on.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_approve_after_the_history_was_pruned_runs_the_run() {
    let Some(db) = TestDb::new("approve_agentless").await else {
        return;
    };
    let run_id = suspended_past_the_history(&db).await;
    let probe = Probe::new("Write", ToolScope::Write, Hold::None);
    let factory = AdminsOnly {
        pool: db.pool.clone(),
        probe: probe.clone(),
    };
    enqueue_approve(&db.pool, "default", NS, &run_id, allow_call_0())
        .await
        .unwrap();
    let shutdown = CancellationToken::new();
    let worker =
        Worker::new(db.pool.clone(), config("w2")).definitions([NS.to_owned()], Arc::new(factory));
    let handle = tokio::spawn(worker.run(shutdown.clone()));
    wait_until("the approved run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    assert_eq!(probe.started(), 1, "the write ran for the admin");
    shutdown.cancel();
    handle.await.unwrap().unwrap();
    // The run ended: the next prune lets go of the row and the definition.
    prune_history(&db.pool, Duration::ZERO).await.unwrap();
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 1);
    db.finish().await;
}

/// A continuation of a run whose definition is gone is refused, rather than
/// queued without an agent that a worker with only a factory never takes.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_continuation_whose_definition_is_gone_is_refused() {
    let Some(db) = TestDb::new("approve_gone").await else {
        return;
    };
    let run_id = suspended_past_the_history(&db).await;
    db.sql("DELETE FROM linch_worker_agents").await;
    let refused = enqueue_approve(&db.pool, "default", NS, &run_id, allow_call_0()).await;
    assert!(
        matches!(refused, Err(WorkerError::Invalid(_))),
        "{refused:?}"
    );
    let resumed = enqueue_resume(&db.pool, "default", NS, &run_id).await;
    assert!(
        matches!(resumed, Err(WorkerError::Invalid(_))),
        "{resumed:?}"
    );
    assert_eq!(db.jobs().await, 0, "nothing was queued");
    db.finish().await;
}

/// A job that failed for good keeps its definition while its run is not
/// terminal, so `enqueue_resume` still has an agent to continue it with.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_failed_job_of_a_live_run_keeps_its_definition() {
    let Some(db) = TestDb::new("failed_live").await else {
        return;
    };
    let run_id = suspended_past_the_history(&db).await;
    // The run's only job record is a failed one: its history row is gone,
    // and the run is still not terminal.
    db.sql("DELETE FROM linch_worker_history").await;
    let mut failed = request("failed", "unused");
    failed.agent = Some("def".into());
    let queued = enqueue(&db.pool, &failed, None).await.unwrap();
    sqlx::query(
        "UPDATE linch_worker_jobs SET failed_at = now() - interval '8 days', \
         error = 'boom', run_id = $1 WHERE id = $2",
    )
    .bind(run_id.as_str())
    .bind(queued.0)
    .execute(&db.pool)
    .await
    .unwrap();
    let week = Duration::from_secs(7 * 24 * 3600);
    assert_eq!(prune_history(&db.pool, week).await.unwrap(), 0);
    assert_eq!(prune_definitions(&db.pool).await.unwrap(), 0);
    let kept: Option<String> = sqlx::query_scalar("SELECT agent FROM linch_worker_jobs")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert!(kept.is_some(), "the failed job lost its definition");
    let id = enqueue_resume(&db.pool, "default", NS, &run_id)
        .await
        .unwrap();
    assert_ne!(id, queued);
    db.finish().await;
}
