//! A due schedule fires once across many pollers and coalesces the fires
//! it missed into one; its fire becomes a run; pause, resume, delete and
//! the cap.

use std::time::Duration;

use chrono::{DateTime, Utc};
use futures::future::join_all;
use linch_core::{Event, RunStatus, SessionId, ToolScope};
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
        let fired = fire_due_schedules(&db.pool, &[]).await.unwrap();
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
        fire_due_schedules(&db.pool, &[]).await.unwrap(),
        0,
        "not due yet"
    );

    // Three ticks overdue, as if every worker had been down.
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '150 minutes'")
        .await;
    let fired = join_all((0..8).map(|_| fire_due_schedules(&db.pool, &[]))).await;
    let fired: u64 = fired.into_iter().map(Result::unwrap).sum();
    // The missed fires coalesce into one, at the first missed tick.
    assert_eq!(fired, 1);
    assert_eq!(fire_all(&db).await, 0);
    let times = fire_times(&db).await;
    assert_eq!(times.len(), 1);
    let hour = chrono::Duration::hours(1);
    let (next, now) = (next_fire(&db, "h").await, now(&db).await);
    assert!(next > now && next <= now + hour, "{next}");
    // The next fire stays on the schedule's grid.
    assert_eq!(next - times[0], hour * 3);
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
    assert_eq!((info.fires, info.skips), (1, 2));
    assert_eq!(info.last_fire, Some(times[0]));
    db.finish().await;
}

/// A schedule 100 ticks overdue (every worker was down) fires once, counts
/// the other 100 as skips, and its next fire is the first tick after now:
/// a heartbeat after a day of downtime is one run, not a storm.
#[tokio::test]
async fn missed_fires_coalesce_into_one() {
    let Some(db) = TestDb::new("coalesce").await else {
        return;
    };
    let mut cron = hourly("cron");
    cron.session_id = None;
    cron.timing = Timing::cron_utc("* * * * *");
    let mut every = hourly("every");
    every.session_id = None;
    every.timing = Timing::Every(Duration::from_secs(60));
    for schedule in [&cron, &every] {
        create_schedule(&db.pool, schedule, None).await.unwrap();
    }
    // On a minute tick, 100 minutes ago: 100 more ticks are due by now.
    db.sql(
        "UPDATE linch_worker_schedules \
         SET next_fire = date_trunc('minute', now()) - interval '100 minutes'",
    )
    .await;
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 2);
    assert_eq!(fire_all(&db).await, 0, "nothing else is due");
    let now = now(&db).await;
    for id in ["cron", "every"] {
        let info = get_schedule(&db.pool, NS, id).await.unwrap().unwrap();
        assert_eq!((info.fires, info.skips), (1, 100), "{id}");
        assert!(info.next_fire > now, "{id}: {}", info.next_fire);
        let minute = chrono::Duration::minutes(1);
        assert!(info.next_fire <= now + minute, "{id}: {}", info.next_fire);
    }
    assert_eq!(db.jobs().await, 2);
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

/// The run a schedule's fire makes names that schedule and the time the
/// fire was due, right after its `init`; a one-off job's run has no such
/// event.
#[tokio::test]
async fn a_fired_run_carries_the_schedule_event() {
    let Some(db) = TestDb::new("fire_event").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h"), None).await.unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let due = next_fire(&db, "h").await;
    let probe = Probe::new("Lookup", ToolScope::Read, Hold::None);
    let worker = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    let run_id = db.run_of("sched").await;
    wait_until("the scheduled run completes", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;

    let events = db.events(&run_id).await;
    let kinds: Vec<_> = events.iter().map(|e| e.event.type_name()).collect();
    assert_eq!(kinds[..3], ["init", "schedule_fire", "user"], "{kinds:?}");
    let Event::ScheduleFire(fire) = &events[1].event else {
        panic!("{:?}", events[1].event);
    };
    assert_eq!(fire.schedule_id, "h");
    assert_eq!(
        i64::try_from(fire.fire_at_ms).unwrap(),
        due.timestamp_millis()
    );
    assert_eq!(events[1].seq, 2);

    // A job that no schedule made has none.
    let session = SessionId::new("plain");
    enqueue_run(&db.pool, "default", NS, &session, "go")
        .await
        .unwrap();
    let plain = db.run_of("plain").await;
    wait_until("the one-off run completes", || async {
        db.status(&plain).await == RunStatus::Completed
    })
    .await;
    let events = db.events(&plain).await;
    assert!(
        events
            .iter()
            .all(|e| e.event.type_name() != "schedule_fire"),
        "{:?}",
        events
            .iter()
            .map(|e| e.event.type_name())
            .collect::<Vec<_>>()
    );
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
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 0);

    // Resuming starts from now: the fires missed while paused are not made.
    let resumed = resume_schedule(&db.pool, NS, "h").await.unwrap().unwrap();
    assert!(!resumed.paused);
    assert!(resumed.next_fire > now(&db).await);
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 0);

    let listed = list_schedules(&db.pool, NS).await.unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(list_schedules(&db.pool, "other").await.unwrap(), vec![]);
    assert!(delete_schedule(&db.pool, NS, "h").await.unwrap());
    assert!(!delete_schedule(&db.pool, NS, "h").await.unwrap());
    assert!(list_schedules(&db.pool, NS).await.unwrap().is_empty());
    db.finish().await;
}

/// The schedule tools see only the schedules of their own agent in their
/// own session: the namespace's schedules whose stored definition is
/// theirs and whose session is the calling run's.
#[tokio::test]
async fn agent_scoped_list_and_delete_see_only_that_definition_and_session() {
    let Some(db) = TestDb::new("agent_scope").await else {
        return;
    };
    let (mine, theirs) = (r#"{"a":1}"#, r#"{"a":2}"#);
    let with = |id: &str, agent: Option<&str>| {
        let mut schedule = hourly(id);
        schedule.agent = agent.map(str::to_owned);
        schedule
    };
    let mut other_session = with("mine-other-session", Some(mine));
    other_session.session_id = Some(SessionId::new("other"));
    let mut no_session = with("mine-no-session", Some(mine));
    no_session.session_id = None;
    for schedule in [
        with("mine", Some(mine)),
        with("theirs", Some(theirs)),
        with("bare", None),
        other_session,
        no_session,
    ] {
        create_schedule(&db.pool, &schedule, None).await.unwrap();
    }
    let mut elsewhere = with("mine-elsewhere", Some(mine));
    elsewhere.namespace = "other".into();
    create_schedule(&db.pool, &elsewhere, None).await.unwrap();

    let session = SessionId::new("sched");
    let listed = list_agent_schedules(&db.pool, NS, mine, &session)
        .await
        .unwrap();
    let ids: Vec<&str> = listed.iter().map(|s| s.id.as_str()).collect();
    assert_eq!(ids, ["mine"]);
    for id in [
        "theirs",
        "bare",
        "mine-elsewhere",
        "mine-other-session",
        "mine-no-session",
    ] {
        let deleted = delete_agent_schedule(&db.pool, NS, id, mine, &session).await;
        assert!(!deleted.unwrap(), "{id}");
    }
    assert_eq!(list_schedules(&db.pool, NS).await.unwrap().len(), 5);
    let delete = || delete_agent_schedule(&db.pool, NS, "mine", mine, &session);
    assert!(delete().await.unwrap());
    assert!(!delete().await.unwrap());
    assert_eq!(list_schedules(&db.pool, NS).await.unwrap().len(), 4);
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
    assert_eq!(fire_due_schedules(&db.pool, &[]).await.unwrap(), 1);
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
/// A capped fire whose root's lock another transaction keeps (a slow
/// `enqueue` of that root) gives up on it after the lock wait: that root's
/// schedule stays due while another root's fires in the same call, and the
/// next call fires the first once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_fire_whose_root_lock_is_held_elsewhere_waits_for_the_next_call() {
    let Some(db) = TestDb::new("fire_lock").await else {
        return;
    };
    let caps = [NS, "p9x"].map(|root| Cap {
        root: root.into(),
        max: 10,
    });
    create_schedule(&db.pool, &hourly("a"), None).await.unwrap();
    let mut elsewhere = hourly("b");
    elsewhere.namespace = "p9x".into();
    create_schedule(&db.pool, &elsewhere, None).await.unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let due = next_fire(&db, "a").await;
    let mut holder = db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("linch_worker_cap:jobs:{NS}"))
        .execute(&mut *holder)
        .await
        .unwrap();
    let started = std::time::Instant::now();
    let skipped = fire_due_schedules(&db.pool, &caps).await;
    let waited = started.elapsed();
    let (jobs, still_due) = (db.jobs().await, next_fire(&db, "a").await);
    holder.rollback().await.unwrap();
    let fired = fire_due_schedules(&db.pool, &caps).await;
    let again = fire_due_schedules(&db.pool, &caps).await;
    let after = db.jobs().await;
    db.finish().await;
    assert!(
        matches!(skipped, Ok(1)),
        "the other root fires: {skipped:?}"
    );
    assert!(waited < Duration::from_secs(5), "waited {waited:?}");
    assert_eq!((jobs, still_due), (1, due), "the held root kept its fire");
    assert!(matches!(fired, Ok(1)), "{fired:?}");
    assert!(matches!(again, Ok(0)), "{again:?}");
    assert_eq!(after, 2, "each schedule fired once");
}

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
