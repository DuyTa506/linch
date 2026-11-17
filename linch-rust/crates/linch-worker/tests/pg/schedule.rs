//! A due schedule fires once across many pollers and catches up the fires
//! it missed; its fire becomes a run; pause, resume, delete and the cap.

use std::time::Duration;

use chrono::{DateTime, Utc};
use futures::future::join_all;
use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{
    Cap, Schedule, Timing, WorkerError, create_schedule, delete_agent_schedule, delete_schedule,
    enqueue_run, fire_due_schedules, get_schedule, list_agent_schedules, list_schedules,
    pause_schedule, resume_schedule,
};

use crate::common::*;

pub fn hourly(id: &str) -> Schedule {
    Schedule {
        namespace: NS.into(),
        id: id.into(),
        queue: "default".into(),
        session_id: Some(SessionId::new("sched")),
        prompt: "tick".into(),
        timing: Timing::cron_utc("0 * * * *"),
        agent: None,
        principal: None,
    }
}

pub async fn now(db: &TestDb) -> DateTime<Utc> {
    sqlx::query_scalar("SELECT now()")
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

async fn next_fire(db: &TestDb, id: &str) -> DateTime<Utc> {
    get_schedule(&db.pool, NS, id)
        .await
        .unwrap()
        .unwrap()
        .next_fire
}

/// Fire until nothing is due; how many jobs that enqueued.
pub async fn fire_all(db: &TestDb) -> u64 {
    let mut total = 0;
    loop {
        let fired = fire_due_schedules(&db.pool).await.unwrap();
        let due: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM linch_worker_schedules \
             WHERE enabled AND next_fire <= now())",
        )
        .fetch_one(&db.pool)
        .await
        .unwrap();
        total += fired;
        if !due {
            return total;
        }
    }
}

async fn fire_times(db: &TestDb) -> Vec<DateTime<Utc>> {
    sqlx::query_scalar("SELECT fire_at FROM linch_worker_jobs ORDER BY fire_at")
        .fetch_all(&db.pool)
        .await
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_due_schedule_fires_once_across_pollers() {
    let Some(db) = TestDb::new("fire").await else {
        return;
    };
    let mut every = hourly("h");
    every.session_id = None;
    every.timing = Timing::Every(Duration::from_secs(3600));
    let created = create_schedule(&db.pool, &every, None).await.unwrap();
    assert!(created.next_fire > now(&db).await);
    assert_eq!(
        fire_due_schedules(&db.pool).await.unwrap(),
        0,
        "not due yet"
    );

    // Three ticks overdue, as if every worker had been down.
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '150 minutes'")
        .await;
    let fired = join_all((0..8).map(|_| fire_due_schedules(&db.pool))).await;
    let fired: u64 = fired.into_iter().map(Result::unwrap).sum();
    assert!((1..=3).contains(&fired), "{fired}");
    // Missed fires are caught up, each once, in order.
    assert_eq!(fired + fire_all(&db).await, 3);
    let times = fire_times(&db).await;
    assert_eq!(times.len(), 3);
    let hour = chrono::Duration::hours(1);
    assert_eq!((times[1] - times[0], times[2] - times[1]), (hour, hour));
    let (next, now) = (next_fire(&db, "h").await, now(&db).await);
    assert!(next > now && next <= now + hour, "{next}");
    let keys: Vec<String> = sqlx::query_scalar("SELECT dedupe_key FROM linch_worker_jobs")
        .fetch_all(&db.pool)
        .await
        .unwrap();
    assert!(
        keys.iter().all(|key| key.starts_with("schedule:h:")),
        "{keys:?}"
    );
    // Without a fixed session, each fire starts its own session.
    let sessions: Vec<String> = sqlx::query_scalar("SELECT session_id FROM linch_worker_jobs")
        .fetch_all(&db.pool)
        .await
        .unwrap();
    assert_eq!(sessions, keys);
    let info = get_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert_eq!((info.fires, info.skips), (3, 0));
    assert_eq!(info.last_fire, Some(times[2]));
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_fire_becomes_a_run() {
    let Some(db) = TestDb::new("fire_run").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    let run_id = db.run_of("sched").await;
    wait_until("the scheduled run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    wait_until("its job is acked", || async { db.jobs().await == 0 }).await;
    assert_eq!(probe.started(), 1);
    worker.stop().await;
    db.finish().await;
}

#[tokio::test]
async fn pause_resume_and_delete() {
    let Some(db) = TestDb::new("pause").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    let taken = create_schedule(&db.pool, &hourly("h"), None).await;
    assert!(matches!(taken, Err(WorkerError::ScheduleExists { id }) if id == "h"));
    let mut bad = hourly("bad");
    bad.timing = Timing::cron_utc("61 * * * *");
    assert!(create_schedule(&db.pool, &bad, None).await.is_err());
    bad.timing = Timing::Cron {
        pattern: "0 9 * * *".into(),
        timezone: "Nowhere/Town".into(),
    };
    assert!(create_schedule(&db.pool, &bad, None).await.is_err());

    let paused = pause_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert!(paused.paused);
    assert!(
        pause_schedule(&db.pool, NS, "nope")
            .await
            .unwrap()
            .is_none()
    );
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '3 hours'")
        .await;
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 0);

    // Resuming starts from now: the fires missed while paused are not made.
    let resumed = resume_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert!(!resumed.paused);
    assert!(resumed.next_fire > now(&db).await);
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 0);

    let listed = list_schedules(&db.pool, NS).await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(list_schedules(&db.pool, "other").await.unwrap(), vec![]);
    assert!(delete_schedule(&db.pool, NS, "h").await.unwrap());
    assert!(!delete_schedule(&db.pool, NS, "h").await.unwrap());
    assert!(list_schedules(&db.pool, NS).await.unwrap().is_empty());
    db.finish().await;
}

/// The schedule tools see only the schedules of their own agent: the
/// namespace's schedules whose stored definition is theirs.
#[tokio::test]
async fn agent_scoped_list_and_delete_see_only_that_definition() {
    let Some(db) = TestDb::new("agent_scope").await else {
        return;
    };
    let (mine, theirs) = (r#"{"a":1}"#, r#"{"a":2}"#);
    let with = |id: &str, agent: Option<&str>| {
        let mut schedule = hourly(id);
        schedule.agent = agent.map(str::to_owned);
        schedule
    };
    for schedule in [
        with("mine", Some(mine)),
        with("theirs", Some(theirs)),
        with("bare", None),
    ] {
        create_schedule(&db.pool, &schedule, None).await.unwrap();
    }
    let mut elsewhere = with("mine-elsewhere", Some(mine));
    elsewhere.namespace = "other".into();
    create_schedule(&db.pool, &elsewhere, None).await.unwrap();

    let listed = list_agent_schedules(&db.pool, NS, mine).await.unwrap();
    let ids: Vec<&str> = listed.iter().map(|s| s.id.as_str()).collect();
    assert_eq!(ids, ["mine"]);
    for id in ["theirs", "bare", "mine-elsewhere"] {
        let deleted = delete_agent_schedule(&db.pool, NS, id, mine).await;
        assert!(!deleted.unwrap(), "{id}");
    }
    assert_eq!(list_schedules(&db.pool, NS).await.unwrap().len(), 3);
    assert!(
        delete_agent_schedule(&db.pool, NS, "mine", mine)
            .await
            .unwrap()
    );
    assert!(
        !delete_agent_schedule(&db.pool, NS, "mine", mine)
            .await
            .unwrap()
    );
    assert_eq!(list_schedules(&db.pool, NS).await.unwrap().len(), 2);
    db.finish().await;
}

#[tokio::test]
async fn a_broken_stored_timing_pauses_only_its_schedule() {
    let Some(db) = TestDb::new("broken").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("good"), None)
        .await
        .unwrap();
    create_schedule(&db.pool, &hourly("broken"), None)
        .await
        .unwrap();
    db.sql("UPDATE linch_worker_schedules SET cron = 'nonsense' WHERE id = 'broken'")
        .await;
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 1);
    let paused: Vec<(String, bool)> = list_schedules(&db.pool, NS)
        .await
        .unwrap()
        .into_iter()
        .map(|s| (s.id, s.paused))
        .collect();
    assert_eq!(
        paused,
        vec![("broken".to_owned(), true), ("good".to_owned(), false)]
    );
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_schedule_cap_counts_the_whole_root() {
    let Some(db) = TestDb::new("cap").await else {
        return;
    };
    let cap = Cap {
        root: NS.into(),
        max: 2,
    };
    create_schedule(&db.pool, &hourly("a"), Some(&cap))
        .await
        .unwrap();
    let mut other = hourly("b");
    other.namespace = format!("{NS}/support");
    create_schedule(&db.pool, &other, Some(&cap)).await.unwrap();
    // Concurrent creates past the cap: the advisory lock lets one count
    // the other's row.
    let more = join_all(["c", "d", "e"].map(|id| {
        let schedule = hourly(id);
        let (pool, cap) = (db.pool.clone(), cap.clone());
        async move { create_schedule(&pool, &schedule, Some(&cap)).await }
    }))
    .await;
    for created in more {
        assert!(matches!(
            created,
            Err(WorkerError::LimitReached {
                what: "schedules",
                max: 2
            })
        ));
    }
    // Another root is not counted.
    let mut elsewhere = hourly("x");
    elsewhere.namespace = "p9x".into();
    let cap_x = Cap {
        root: "p9x".into(),
        max: 1,
    };
    create_schedule(&db.pool, &elsewhere, Some(&cap_x))
        .await
        .unwrap();
    db.finish().await;
}

/// Run a start job per session in `sessions`, and wait until they end.
async fn run_jobs(db: &TestDb, probe: &Probe, sessions: std::ops::Range<u32>) {
    for i in sessions.clone() {
        let session = SessionId::new(format!("s{i}"));
        enqueue_run(&db.pool, "default", NS, &session, "go")
            .await
            .unwrap();
    }
    wait_until("the jobs end", || async {
        probe.started() >= sessions.end && db.jobs().await == 0
    })
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_worker_checks_schedules_once_per_poll_interval() {
    let Some(db) = TestDb::new("throttle").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    let mut slow = config("w1");
    slow.poll_interval = Duration::from_secs(30);
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, slow, agent(&db.pool, probe.clone()));
    // A job the worker ran: it has polled, and checked the schedules.
    run_jobs(&db, &probe, 0..1).await;
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    // Every enqueue wakes the worker, but the schedules wait for the poll
    // interval, so the overdue fire stays due.
    run_jobs(&db, &probe, 1..6).await;
    let (next, now) = (next_fire(&db, "h").await, now(&db).await);
    assert!(next < now, "the schedule fired on a wake");
    worker.stop().await;
    db.finish().await;
}
