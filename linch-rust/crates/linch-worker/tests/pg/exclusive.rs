//! Two workers never run one job at once, and a queue's limit, stored with
//! the queue, holds across the fleet.

use std::time::Duration;

use linch_core::{RunStatus, SessionId, ToolScope};
use linch_worker::{enqueue_run, set_queue_limit};

use crate::common::*;

async fn completed(db: &TestDb, sessions: &[String]) -> usize {
    let mut done = 0;
    for session in sessions {
        for run_id in db.runs_of(session).await {
            if db.status(&run_id).await == RunStatus::Completed {
                done += 1;
            }
        }
    }
    done
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_workers_never_run_one_job_at_once() {
    let Some(db) = TestDb::new("exclusive").await else {
        return;
    };
    let probe = Probe::new(
        "Lookup",
        ToolScope::Read,
        Hold::Sleep(Duration::from_millis(30)),
    );
    let sessions: Vec<String> = (0..12).map(|i| format!("s{i}")).collect();
    for session in &sessions {
        let session_id = SessionId::new(session.as_str());
        enqueue_run(&db.pool, "default", NS, &session_id, "go")
            .await
            .unwrap();
    }
    let workers = [
        Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone())),
        Running::spawn(&db.pool, config("w2"), agent(&db.pool, probe.clone())),
    ];
    wait_until("every run completes", || async {
        completed(&db, &sessions).await == sessions.len()
    })
    .await;
    wait_until("every job is acked", || async { db.jobs().await == 0 }).await;
    for session in &sessions {
        let run_id = db.run_of(session).await;
        // One claim: no second worker ever took the run over.
        assert_eq!(db.record(&run_id).await.epoch, 1, "{session}");
    }
    {
        let stats = probe.stats.lock().unwrap();
        assert_eq!(stats.max_per_run, 1);
        assert_eq!(stats.runs.len(), sessions.len());
        let once = stats.runs.values().all(|&calls| calls == 1);
        assert!(once, "{:?}", stats.runs);
        // Calls ran side by side, so the checks above meant something.
        assert!(stats.max_total > 1, "the calls never overlapped");
    }
    for worker in workers {
        worker.stop().await;
    }
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_queue_limit_holds_across_workers() {
    let Some(db) = TestDb::new("limit").await else {
        return;
    };
    let probe = Probe::new(
        "Lookup",
        ToolScope::Read,
        Hold::Sleep(Duration::from_millis(150)),
    );
    let sessions: Vec<String> = (0..4).map(|i| format!("s{i}")).collect();
    for session in &sessions {
        let session_id = SessionId::new(session.as_str());
        enqueue_run(&db.pool, "narrow", NS, &session_id, "go")
            .await
            .unwrap();
    }
    // The limit is the queue's: neither worker is configured with it.
    set_queue_limit(&db.pool, "narrow", 1).await.unwrap();
    let narrow = |owner: &str| {
        let mut config = config(owner);
        config.queues = vec!["narrow".into()];
        config
    };
    let workers = [
        Running::spawn(&db.pool, narrow("w1"), agent(&db.pool, probe.clone())),
        Running::spawn(&db.pool, narrow("w2"), agent(&db.pool, probe.clone())),
    ];
    wait_until("every run completes", || async {
        completed(&db, &sessions).await == sessions.len()
    })
    .await;
    assert_eq!(probe.stats.lock().unwrap().max_total, 1);
    for worker in workers {
        worker.stop().await;
    }
    db.finish().await;
}
