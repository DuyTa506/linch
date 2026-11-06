//! A crashed or stalled worker's job becomes visible again; the next worker
//! resumes the run, and the epoch fences the old one out.

use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use futures::StreamExt;
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::{Envelope, Event, RunOptions, RunStatus, SessionId, ToolScope};
use linch_worker::{Worker, WorkerConfig, enqueue_run};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::Semaphore;
use tokio::time::Instant;
use tokio_util::sync::{CancellationToken, DropGuard};

use crate::common::*;

/// How many tool ends the run's events hold.
fn tool_ends(events: &[Envelope]) -> usize {
    let is_end = |envelope: &&Envelope| matches!(envelope.event, Event::ToolCallEnd(_));
    events.iter().filter(is_end).count()
}

/// A probe whose first call blocks its thread until the returned sender
/// sends or drops.
fn freezing_probe() -> (Probe, mpsc::Sender<()>) {
    let (release, frozen) = mpsc::channel();
    let hold = Hold::Freeze(Arc::new(Mutex::new(Some(frozen))));
    (Probe::new("Lookup", ToolScope::Read, hold), release)
}

/// A worker on its own current-thread runtime and pool, so a frozen tool
/// stalls all of it: no heartbeat, no progress, like a long pause.
struct FrozenWorker {
    stop: CancellationToken,
    _stop_on_exit: DropGuard,
    done: mpsc::Receiver<Result<(), String>>,
}

impl FrozenWorker {
    fn spawn(db: &TestDb, probe: Probe, config: WorkerConfig) -> Self {
        let stop = CancellationToken::new();
        let (done_tx, done) = mpsc::channel();
        let (options, token) = (db.options.clone(), stop.clone());
        std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let result = runtime.block_on(async move {
                let pool = PgPoolOptions::new()
                    .max_connections(4)
                    .connect_with(options)
                    .await
                    .unwrap();
                let worker = Worker::new(pool.clone(), config).agent(agent(&pool, probe));
                let result = worker.run(token).await;
                pool.close().await;
                result
            });
            let _ = done_tx.send(result.map_err(|error| error.to_string()));
        });
        Self {
            _stop_on_exit: stop.clone().drop_guard(),
            stop,
            done,
        }
    }

    /// Stop it and wait for its jobs in flight.
    fn finish(self) -> Result<(), String> {
        self.stop.cancel();
        let wait = Duration::from_secs(20);
        self.done
            .recv_timeout(wait)
            .expect("the worker thread stops")
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_killed_workers_job_is_taken_over() {
    let Some(db) = TestDb::new("killed").await else {
        return;
    };
    let probe = Probe::new(
        "Lookup",
        ToolScope::Read,
        Hold::Forever(Arc::new(Semaphore::new(0))),
    );
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let first = Running::spawn(&db.pool, config("w1"), agent(&db.pool, probe.clone()));
    wait_until("the first worker runs the tool", || async {
        probe.started() == 1
    })
    .await;
    let run_id = db.run_of("s1").await;
    first.kill().await;
    let killed = Instant::now();

    let second = Running::spawn(
        &db.pool,
        config("w2"),
        agent(&db.pool, probe.with_hold(Hold::None)),
    );
    wait_until("the second worker completes the run", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    // The job stayed hidden until its heartbeat-extended timeout ran out.
    assert!(
        killed.elapsed() >= TIMEOUT / 3,
        "taken over before the timeout"
    );
    let record = db.record(&run_id).await;
    assert_eq!(record.epoch, 2, "one claim per worker");
    // The call started on the killed worker is not run again: the model
    // gets the interrupted placeholder, and no end was ever recorded.
    assert_eq!(probe.started(), 1);
    assert_eq!(db.tool_results("s1").await, vec![INTERRUPTED_TOOL_TEXT]);
    assert_eq!(tool_ends(&db.events(&run_id).await), 0);
    wait_until("the job is acked", || async { db.jobs().await == 0 }).await;
    second.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_stalled_worker_is_taken_over_and_writes_nothing() {
    let Some(db) = TestDb::new("stalled").await else {
        return;
    };
    let (probe, release) = freezing_probe();
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let first = FrozenWorker::spawn(&db, probe.clone(), config("stalled"));
    wait_until("the first worker runs the tool", || async {
        probe.started() == 1
    })
    .await;
    let run_id = db.run_of("s1").await;

    let second = Running::spawn(
        &db.pool,
        config("w2"),
        agent(&db.pool, probe.with_hold(Hold::None)),
    );
    wait_until("the second worker completes the run", || async {
        db.status(&run_id).await == RunStatus::Completed
    })
    .await;
    wait_until("the job is acked", || async { db.jobs().await == 0 }).await;
    let before = db.events(&run_id).await;
    assert_eq!(tool_ends(&before), 0);
    assert_eq!(db.tool_results("s1").await, vec![INTERRUPTED_TOOL_TEXT]);

    // Thaw the first worker. Its last confirmed heartbeat is older than the
    // timeout, so it drops the run before the run writes again.
    release.send(()).unwrap();
    assert_eq!(first.finish(), Ok(()));
    assert_eq!(
        db.events(&run_id).await,
        before,
        "the stalled worker wrote nothing"
    );
    assert_eq!(db.tool_results("s1").await, vec![INTERRUPTED_TOOL_TEXT]);
    let record = db.record(&run_id).await;
    assert_eq!((record.status, record.epoch), (RunStatus::Completed, 2));
    assert_eq!(probe.started(), 1);
    assert_eq!(db.jobs().await, 0, "no job came back");
    second.stop().await;
    db.finish().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_claim_elsewhere_fences_a_worker_that_still_holds_its_job() {
    let Some(db) = TestDb::new("fenced").await else {
        return;
    };
    let (probe, release) = freezing_probe();
    enqueue_run(&db.pool, "default", NS, &SessionId::new("s1"), "go")
        .await
        .unwrap();
    let mut slow_retry = config("held");
    slow_retry.retry_delay = Duration::from_secs(60);
    let worker = FrozenWorker::spawn(&db, probe.clone(), slow_retry);
    wait_until("the worker runs the tool", || async {
        probe.started() == 1
    })
    .await;
    let run_id = db.run_of("s1").await;

    // While the worker pauses briefly (well inside its job's timeout), the
    // host resumes the run itself: its claim takes epoch 2.
    let host = agent(&db.pool, probe.with_hold(Hold::None));
    let mut options = RunOptions::new("s1");
    options.owner = "host".into();
    let mut stream = host.resume(run_id.clone(), options);
    while let Some(item) = stream.next().await {
        item.expect("the host's resume");
    }
    assert_eq!(db.status(&run_id).await, RunStatus::Completed);
    let before = db.events(&run_id).await;

    // The worker's heartbeat still holds the job, but its next run write
    // carries epoch 1 and is refused; the job goes back with that error.
    release.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    let error = loop {
        let row: (Option<String>, Option<String>) =
            sqlx::query_as("SELECT error, owner FROM linch_worker_jobs")
                .fetch_one(&db.pool)
                .await
                .unwrap();
        if let (Some(error), None) = row {
            break error;
        }
        assert!(Instant::now() < deadline, "the job was never released");
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    assert!(
        error.contains("lease on run") && error.contains("lost"),
        "{error}"
    );
    assert_eq!(worker.finish(), Ok(()));
    assert_eq!(
        db.events(&run_id).await,
        before,
        "the fenced worker wrote nothing"
    );
    let record = db.record(&run_id).await;
    assert_eq!((record.status, record.epoch), (RunStatus::Completed, 2));
    db.finish().await;
}
