//! A due schedule fires once across many pollers, and its fire becomes a
//! run.

use chrono::{DateTime, Utc};
use futures::future::join_all;
use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{Schedule, cancel_schedule, create_schedule, fire_due_schedules};

use crate::common::*;

fn hourly(id: &str) -> Schedule {
    Schedule {
        namespace: NS.into(),
        id: id.into(),
        queue: "default".into(),
        session_id: SessionId::new("sched"),
        prompt: "tick".into(),
        cron: "0 * * * *".into(),
    }
}

async fn now(db: &TestDb) -> DateTime<Utc> {
    sqlx::query_scalar("SELECT now()")
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

async fn next_fire(db: &TestDb, id: &str) -> DateTime<Utc> {
    sqlx::query_scalar("SELECT next_fire FROM linch_worker_schedules WHERE id = $1")
        .bind(id)
        .fetch_one(&db.pool)
        .await
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_due_schedule_fires_once_across_pollers() {
    let Some(db) = TestDb::new("fire").await else {
        return;
    };
    let first = create_schedule(&db.pool, &hourly("h")).await.unwrap();
    assert!(first > now(&db).await);
    assert_eq!(
        fire_due_schedules(&db.pool).await.unwrap(),
        0,
        "not due yet"
    );

    // Three ticks overdue, as if every worker had been down.
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '3 hours'")
        .await;
    let fired = join_all((0..8).map(|_| fire_due_schedules(&db.pool))).await;
    let fired: u64 = fired.into_iter().map(Result::unwrap).sum();
    assert_eq!(fired, 1, "one fire across eight pollers");
    let keys: Vec<String> = sqlx::query_scalar("SELECT dedupe_key FROM linch_worker_jobs")
        .fetch_all(&db.pool)
        .await
        .unwrap();
    assert_eq!(keys.len(), 1);
    assert!(keys[0].starts_with("schedule:h:"), "{keys:?}");
    // Missed ticks are not replayed: the next fire is the next tick from now.
    let (next, now) = (next_fire(&db, "h").await, now(&db).await);
    assert!(
        next > now && next <= now + chrono::Duration::hours(1),
        "{next}"
    );
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 0);

    // The fire becomes a run in the schedule's session.
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
async fn a_cancelled_schedule_does_not_fire() {
    let Some(db) = TestDb::new("cancel").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("h")).await.unwrap();
    assert!(
        create_schedule(&db.pool, &hourly("h")).await.is_err(),
        "ids are unique"
    );
    let mut bad = hourly("bad");
    bad.cron = "61 * * * *".into();
    assert!(create_schedule(&db.pool, &bad).await.is_err());
    assert!(cancel_schedule(&db.pool, NS, "h").await.unwrap());
    assert!(!cancel_schedule(&db.pool, NS, "h").await.unwrap());
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 0);
    assert_eq!(db.jobs().await, 0);
    db.finish().await;
}

#[tokio::test]
async fn a_broken_stored_pattern_disables_only_its_schedule() {
    let Some(db) = TestDb::new("broken").await else {
        return;
    };
    create_schedule(&db.pool, &hourly("good")).await.unwrap();
    create_schedule(&db.pool, &hourly("broken")).await.unwrap();
    db.sql("UPDATE linch_worker_schedules SET cron = 'nonsense' WHERE id = 'broken'")
        .await;
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    assert_eq!(fire_due_schedules(&db.pool).await.unwrap(), 1);
    let enabled: Vec<(String, bool)> =
        sqlx::query_as("SELECT id, enabled FROM linch_worker_schedules ORDER BY id")
            .fetch_all(&db.pool)
            .await
            .unwrap();
    assert_eq!(
        enabled,
        vec![("broken".to_owned(), false), ("good".to_owned(), true)]
    );
    db.finish().await;
}
