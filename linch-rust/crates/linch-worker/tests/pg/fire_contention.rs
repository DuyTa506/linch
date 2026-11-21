//! Schedule fires against a capped root's lock: under steady capped-enqueue
//! traffic, with a held root at the head of the due batch, and exactly once
//! across firers while a root's lock comes and goes.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use linch_core::SessionId;
use linch_worker::{
    Cap, RunRequest, Schedule, Timing, create_schedule, enqueue, fire_due_schedules,
};
use tokio::time::Instant;

use crate::common::*;

fn schedule_in(namespace: &str, id: &str) -> Schedule {
    Schedule {
        namespace: namespace.into(),
        id: id.into(),
        queue: "default".into(),
        session_id: None,
        prompt: "tick".into(),
        timing: Timing::Every(Duration::from_secs(3600)),
        agent: None,
        principal: None,
    }
}

/// `clients` tasks that enqueue capped runs of `root` back to back until
/// `stop`; how many landed.
fn enqueuers(
    db: &TestDb,
    root: &str,
    clients: u32,
    stop: &Arc<AtomicBool>,
    landed: &Arc<AtomicU64>,
) -> Vec<tokio::task::JoinHandle<()>> {
    (0..clients)
        .map(|client| {
            let (pool, root) = (db.pool.clone(), root.to_owned());
            let (stop, landed) = (Arc::clone(stop), Arc::clone(landed));
            tokio::spawn(async move {
                let cap = Cap {
                    root: root.clone(),
                    max: 1_000_000,
                };
                let mut i = 0u64;
                while !stop.load(Ordering::Relaxed) {
                    i += 1;
                    let request = RunRequest {
                        queue: "other".into(),
                        namespace: format!("{root}/a"),
                        session_id: SessionId::new(format!("c{client}-{i}")),
                        prompt: "go".into(),
                        agent: None,
                        principal: None,
                    };
                    enqueue(&pool, &request, Some(&cap)).await.unwrap();
                    landed.fetch_add(1, Ordering::Relaxed);
                }
            })
        })
        .collect()
}

/// Fire every 100 ms for 5 s while `clients` clients enqueue capped runs of
/// the schedule's root back to back (no slow transaction anywhere); the
/// number of calls until the schedule fired, or `None`, and how many
/// enqueues landed meanwhile.
async fn fires_under_enqueue_traffic(name: &str, clients: u32) -> (Option<u32>, u64) {
    let db = TestDb::new(name).await.expect("LINCH_TEST_PG_DSN is set");
    let root = format!("busy{}{name}", std::process::id());
    let caps = [Cap {
        root: root.clone(),
        max: 1_000_000,
    }];
    create_schedule(&db.pool, &schedule_in(&format!("{root}/a"), "tick"), None)
        .await
        .unwrap();
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let (stop, landed) = (
        Arc::new(AtomicBool::new(false)),
        Arc::new(AtomicU64::new(0)),
    );
    let clients = enqueuers(&db, &root, clients, &stop, &landed);
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut fired_at = None;
    for call in 1..=50 {
        let fired = fire_due_schedules(&db.pool, &caps).await.unwrap();
        if fired > 0 {
            fired_at = Some(call);
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    stop.store(true, Ordering::Relaxed);
    for client in clients {
        client.await.unwrap();
    }
    db.finish().await;
    (fired_at, landed.load(Ordering::Relaxed))
}

/// A due schedule of a capped root fires while four clients enqueue runs
/// of that root back to back: the fire has to get the root's lock in turn.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_schedule_fires_while_four_clients_enqueue_into_its_root() {
    if std::env::var("LINCH_TEST_PG_DSN").is_err() {
        eprintln!("skipping: LINCH_TEST_PG_DSN is not set");
        return;
    }
    let (fired_at, landed) = fires_under_enqueue_traffic("busy4", 4).await;
    assert!(
        fired_at.is_some(),
        "50 calls in 5 s never fired the due schedule ({landed} enqueues landed meanwhile)"
    );
}

/// "Held roots stay due, the other roots fire": with a full batch of due
/// schedules of the held root ahead of it, another root's schedule fires
/// too.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn another_root_fires_behind_a_full_batch_of_a_held_root() {
    let Some(db) = TestDb::new("head_of_line").await else {
        return;
    };
    let held = format!("held{}", std::process::id());
    let free = format!("free{}", std::process::id());
    let caps = [Cap {
        root: held.clone(),
        max: 1000,
    }];
    for i in 0..64 {
        let schedule = schedule_in(&format!("{held}/a"), &format!("h{i}"));
        create_schedule(&db.pool, &schedule, None).await.unwrap();
    }
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '2 seconds'")
        .await;
    create_schedule(&db.pool, &schedule_in(&format!("{free}/a"), "f"), None)
        .await
        .unwrap();
    db.sql(
        "UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second' \
         WHERE id = 'f'",
    )
    .await;
    let mut holder = db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(format!("linch_worker_cap:jobs:{held}"))
        .execute(&mut *holder)
        .await
        .unwrap();
    let mut fired = 0;
    for _ in 0..5 {
        let call = fire_due_schedules(&db.pool, &caps);
        fired += match tokio::time::timeout(Duration::from_secs(3), call).await {
            Ok(fired) => fired.unwrap(),
            Err(_) => 0,
        };
    }
    holder.rollback().await.unwrap();
    db.finish().await;
    assert_eq!(
        fired, 1,
        "the other root's schedule did not fire in 5 calls while the held root's 64 were due"
    );
}

/// Exactly once: four firers call in a loop while a root's lock is taken
/// and released by another transaction; every schedule gets one job.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn firers_racing_a_flapping_root_lock_fire_each_schedule_once() {
    let Some(db) = TestDb::new("flap").await else {
        return;
    };
    let roots = [0, 1].map(|i| format!("flap{i}x{}", std::process::id()));
    let caps = roots.clone().map(|root| Cap { root, max: 1000 });
    for root in &roots {
        for i in 0..10 {
            let schedule = schedule_in(&format!("{root}/a"), &format!("s{i}"));
            create_schedule(&db.pool, &schedule, None).await.unwrap();
        }
    }
    db.sql("UPDATE linch_worker_schedules SET next_fire = now() - interval '1 second'")
        .await;
    let stop = Arc::new(AtomicBool::new(false));
    let flapper = {
        let (pool, root, stop) = (db.pool.clone(), roots[0].clone(), Arc::clone(&stop));
        tokio::spawn(async move {
            while !stop.load(Ordering::Relaxed) {
                let mut holder = pool.begin().await.unwrap();
                sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
                    .bind(format!("linch_worker_cap:jobs:{root}"))
                    .execute(&mut *holder)
                    .await
                    .unwrap();
                tokio::time::sleep(Duration::from_millis(7)).await;
                holder.rollback().await.unwrap();
                tokio::time::sleep(Duration::from_millis(3)).await;
            }
        })
    };
    let firers: Vec<_> = (0..4)
        .map(|_| {
            let (pool, caps) = (db.pool.clone(), caps.clone());
            tokio::spawn(async move {
                let until = Instant::now() + Duration::from_secs(3);
                let mut total = 0;
                while Instant::now() < until {
                    total += fire_due_schedules(&pool, &caps).await.unwrap();
                }
                total
            })
        })
        .collect();
    let mut total = 0;
    for firer in firers {
        total += firer.await.unwrap();
    }
    stop.store(true, Ordering::Relaxed);
    flapper.await.unwrap();
    let per_schedule: Vec<i64> = sqlx::query_scalar(
        "SELECT count(j.id) FROM linch_worker_schedules AS s \
         LEFT JOIN linch_worker_jobs AS j \
           ON j.schedule_id = s.id AND j.namespace = s.namespace \
         GROUP BY s.namespace, s.id",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    db.finish().await;
    assert_eq!(per_schedule, vec![1; 20], "one job per schedule");
    assert_eq!(total, 20, "the calls reported each fire once");
}
