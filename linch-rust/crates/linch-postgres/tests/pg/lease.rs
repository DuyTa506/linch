//! Two agent handles on one database stand for two server processes: one
//! drives a run, the other tries to resume it. Each handle has its own
//! pool and its own `PgRunStore`, so each is its own lease holder.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Commit, Envelope, Event, JournalError, Lease, NewRun,
    PermissionEngine, PermissionMode, PermissionPolicy, Provider, RunError, RunId, RunOptions,
    RunRecord, RunStatus, RunStore, RunStream, ScriptedProvider, StoreError,
};
use linch_postgres::{PgRunStore, PgSessionStore};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::{Notify, Semaphore};

use crate::common::TestDb;
use crate::idle::Gated;

const MODEL: &str = "scripted-model";

/// A short lease, so the tests that wait one out stay quick.
const TTL: Duration = Duration::from_millis(600);

/// A run store whose renewals stop landing once frozen: the process behind
/// it stands for one that crashed or froze, which no longer beats.
struct Freezable {
    inner: PgRunStore,
    frozen: Arc<AtomicBool>,
}

#[async_trait]
impl RunStore for Freezable {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.inner.create(run).await
    }

    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.inner.claim(ns, run_id, owner).await
    }

    async fn seize(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.inner.seize(ns, run_id, owner).await
    }

    fn lease_ttl(&self) -> Option<Duration> {
        self.inner.lease_ttl()
    }

    async fn renew(&self, lease: &Lease) -> Result<(), StoreError> {
        if self.frozen.load(Ordering::SeqCst) {
            return Err(StoreError::Backend("the process is frozen".into()));
        }
        self.inner.renew(lease).await
    }

    async fn release(&self, lease: &Lease) -> Result<(), StoreError> {
        self.inner.release(lease).await
    }

    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        self.inner.load(ns, run_id).await
    }

    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        self.inner.commit(lease, commit).await
    }

    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after_seq: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.inner.events(ns, run_id, after_seq).await
    }
}

/// One process: its provider (held at a gate) and its agent, on a pool of
/// its own.
struct Process {
    provider: Arc<Gated>,
    agent: Agent,
    frozen: Arc<AtomicBool>,
}

impl Process {
    /// A process whose model calls answer `text`, `turns` times, once let
    /// through the gate; `ttl` sets its run leases (the default when
    /// `None`).
    async fn new(db: &TestDb, text: &str, turns: usize, ttl: Option<Duration>) -> Self {
        let pool = PgPoolOptions::new()
            .max_connections(3)
            .connect_with(db.options.clone())
            .await
            .expect("connect");
        let turn = || TurnBuilder::new(MODEL).text(text).build();
        let provider = Arc::new(Gated {
            inner: ScriptedProvider::new((0..turns).map(|_| turn())),
            entered: Notify::new(),
            open: Semaphore::new(0),
        });
        let frozen = Arc::new(AtomicBool::new(false));
        let runs = PgRunStore::new(pool.clone());
        let runs = match ttl {
            Some(ttl) => runs.with_lease_ttl(ttl),
            None => runs,
        };
        let policy = PermissionPolicy::new(PermissionMode::Default, "/");
        let agent = AgentBuilder::new(AgentSpec::new(MODEL))
            .provider(Arc::clone(&provider) as Arc<dyn Provider>)
            .permissions(PermissionEngine::new(policy).unwrap())
            .run_store(Arc::new(Freezable {
                inner: runs,
                frozen: Arc::clone(&frozen),
            }))
            .session_store(Arc::new(PgSessionStore::new(pool)))
            .namespace("ns")
            .build()
            .unwrap();
        Self {
            provider,
            agent,
            frozen,
        }
    }

    /// The process stops beating, as if it froze.
    fn freeze(&self) {
        self.frozen.store(true, Ordering::SeqCst);
    }

    /// Let every model call through.
    fn open(&self) {
        self.provider.open.add_permits(Semaphore::MAX_PERMITS);
    }

    fn calls(&self) -> usize {
        self.provider.inner.requests().len()
    }

    /// Resume `run_id` and collect the whole stream.
    async fn resume(&self, run_id: &RunId) -> Vec<Result<Envelope, RunError>> {
        let options = RunOptions::new("s1");
        self.agent.resume(run_id.clone(), options).collect().await
    }
}

/// Start a run on `process` and poll it until its model call waits at the
/// gate; the stream is handed back unpolled.
async fn at_the_gate(process: &Process) -> RunStream {
    let mut stream = process.agent.run("hi", RunOptions::new("s1"));
    let polled = async {
        while let Some(item) = stream.next().await {
            item.expect("the run goes on until the gate");
        }
    };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = process.provider.entered.notified() => {}
    }
    stream
}

/// Poll `stream` for `long` while its model call stays at the gate.
async fn poll_for(stream: &mut RunStream, long: Duration) {
    let polled = async {
        while let Some(item) = stream.next().await {
            item.expect("the run waits at the gate");
        }
        panic!("the gate holds the run");
    };
    let _ = tokio::time::timeout(long, polled).await;
}

fn outcome(items: &[Result<Envelope, RunError>]) -> String {
    match items.last() {
        Some(Ok(envelope)) => match &envelope.event {
            Event::Result(result) => format!("result {:?}", result.subtype),
            other => format!("ended on {other:?}"),
        },
        Some(Err(error)) => format!("error {error:?}"),
        None => "no item".into(),
    }
}

/// Whether `items` is a resume refused with `RunLeased`.
fn refused(items: &[Result<Envelope, RunError>]) -> bool {
    matches!(items, [Err(RunError::Store(StoreError::RunLeased { .. }))])
}

/// The one item of a refused resume: `RunLeased`, naming the run and the
/// holder's owner. Returns how long the lease has left.
fn leased(items: &[Result<Envelope, RunError>], run_id: &RunId) -> Duration {
    match items {
        [
            Err(RunError::Store(StoreError::RunLeased {
                run_id: leased,
                owner,
                expires_in,
            })),
        ] => {
            assert_eq!((leased, owner.as_str()), (run_id, "linch"));
            *expires_in
        }
        _ => panic!("a resume of a live run is RunLeased: {}", outcome(items)),
    }
}

fn lease_lost(items: &[Result<Envelope, RunError>]) -> bool {
    matches!(
        items.last(),
        Some(Err(RunError::Journal(JournalError::Store(
            StoreError::LeaseLost { .. }
        ))))
    )
}

/// The texts of the run's durable `assistant` events.
async fn answers(db: &TestDb, run_id: &RunId) -> Vec<String> {
    let store = PgRunStore::new(db.pool.clone());
    let events = store.events("ns", run_id, 0).await.unwrap();
    let texts = events
        .into_iter()
        .filter_map(|envelope| match envelope.event {
            Event::Assistant(assistant) => Some(assistant.message.text()),
            _ => None,
        });
    texts.collect()
}

async fn record(db: &TestDb, run_id: &RunId) -> (RunStatus, u64) {
    let store = PgRunStore::new(db.pool.clone());
    let record = store.load("ns", run_id).await.unwrap();
    (record.status, record.epoch)
}

/// B resumes while A's model call is in flight. B is refused and writes
/// nothing, so the call is paid once and A's run completes.
#[tokio::test]
async fn a_live_holder_keeps_its_run() {
    let Some(db) = TestDb::migrated("lease_live").await else {
        return;
    };
    let a = Process::new(&db, "from a", 1, None).await;
    let b = Process::new(&db, "from b", 1, None).await;
    b.open();
    let stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    let task_a = tokio::spawn(stream_a.collect::<Vec<_>>());
    let left = leased(&b.resume(&run_id).await, &run_id);
    assert!(left > Duration::from_secs(25), "the default TTL: {left:?}");
    assert_eq!(record(&db, &run_id).await.1, 1, "B wrote nothing");
    a.open();
    assert_eq!(outcome(&task_a.await.unwrap()), "result Success");
    assert_eq!((a.calls(), b.calls()), (1, 0), "the call is paid once");
    assert_eq!(answers(&db, &run_id).await, ["from a"]);
    db.finish().await;
}

/// A's process is gone (it no longer beats, and its stream is leaked, so
/// nothing releases its lease). B is refused until the lease runs out,
/// then takes the run over.
#[tokio::test]
async fn a_dead_holders_run_is_taken_over_after_its_lease_runs_out() {
    let Some(db) = TestDb::migrated("lease_dead").await else {
        return;
    };
    let a = Process::new(&db, "from a", 1, Some(TTL)).await;
    let b = Process::new(&db, "from b", 1, Some(TTL)).await;
    b.open();
    let stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    let stopped = Instant::now();
    a.freeze();
    std::mem::forget(stream_a);
    leased(&b.resume(&run_id).await, &run_id);
    let items = loop {
        let items = b.resume(&run_id).await;
        if !refused(&items) {
            break items;
        }
        assert!(stopped.elapsed() < TTL * 20, "the lease never ran out");
        tokio::time::sleep(TTL / 10).await;
    };
    assert!(stopped.elapsed() >= TTL / 2, "{:?}", stopped.elapsed());
    assert_eq!(outcome(&items), "result Success");
    assert_eq!(record(&db, &run_id).await, (RunStatus::Completed, 2));
    assert_eq!(answers(&db, &run_id).await, ["from b"]);
    db.finish().await;
}

/// The heartbeat beats on a timer: a model call a lease and a half long,
/// with no write in between, keeps the run A's. The lease is long enough
/// that a beat a loaded disk delays by a couple of seconds is still on time.
#[tokio::test]
async fn a_long_model_call_keeps_its_lease() {
    let Some(db) = TestDb::migrated("lease_long_call").await else {
        return;
    };
    let ttl = Duration::from_secs(6);
    let a = Process::new(&db, "from a", 1, Some(ttl)).await;
    let b = Process::new(&db, "from b", 1, Some(ttl)).await;
    b.open();
    let mut stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    poll_for(&mut stream_a, ttl * 3 / 2).await;
    leased(&b.resume(&run_id).await, &run_id);
    a.open();
    let items: Vec<_> = stream_a.collect().await;
    assert_eq!(outcome(&items), "result Success");
    assert_eq!((a.calls(), b.calls()), (1, 0));
    db.finish().await;
}

/// A stalls past its lease (a long pause: it stops beating), and B takes
/// the run over and finishes it. When A wakes, its receipt write is
/// fenced: the run stops in A and B's state stands.
#[tokio::test]
async fn a_holder_that_lost_its_lease_cannot_write() {
    let Some(db) = TestDb::migrated("lease_fenced").await else {
        return;
    };
    let a = Process::new(&db, "from a", 1, Some(TTL)).await;
    let b = Process::new(&db, "from b", 1, Some(TTL)).await;
    b.open();
    let stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    a.freeze();
    tokio::time::sleep(TTL + Duration::from_millis(200)).await;
    assert_eq!(outcome(&b.resume(&run_id).await), "result Success");
    a.open();
    let items: Vec<_> = stream_a.collect().await;
    assert!(lease_lost(&items), "A is fenced: {}", outcome(&items));
    assert_eq!(record(&db, &run_id).await, (RunStatus::Completed, 2));
    assert_eq!(
        answers(&db, &run_id).await,
        ["from b"],
        "nothing of A landed"
    );
    db.finish().await;
}

/// `Agent::fail` from another process takes a live run over: the holder's
/// next heartbeat finds the lease gone and stops the run at once, with its
/// model call still in flight.
#[tokio::test]
async fn failing_a_run_elsewhere_stops_its_live_holder() {
    let Some(db) = TestDb::migrated("lease_failed").await else {
        return;
    };
    let a = Process::new(&db, "from a", 1, Some(TTL)).await;
    let b = Process::new(&db, "from b", 1, Some(TTL)).await;
    let mut stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    let options = RunOptions::new("s1");
    b.agent
        .fail(run_id.clone(), "stopped", options)
        .await
        .unwrap();
    let mut items = Vec::new();
    let drained = async {
        while let Some(item) = stream_a.next().await {
            items.push(item);
        }
    };
    tokio::time::timeout(TTL * 10, drained)
        .await
        .expect("the heartbeat stops the run without the gate opening");
    assert!(lease_lost(&items), "{}", outcome(&items));
    assert_eq!(record(&db, &run_id).await, (RunStatus::Failed, 2));
    db.finish().await;
}

/// A dropped stream releases its lease, so another process need not wait
/// out the default 30 s.
#[tokio::test]
async fn a_dropped_run_is_free_at_once() {
    let Some(db) = TestDb::migrated("lease_dropped").await else {
        return;
    };
    let a = Process::new(&db, "from a", 1, None).await;
    let b = Process::new(&db, "from b", 1, None).await;
    b.open();
    let stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    let dropped = Instant::now();
    drop(stream_a);
    let items = loop {
        let items = b.resume(&run_id).await;
        if !refused(&items) {
            break items;
        }
        assert!(dropped.elapsed() < Duration::from_secs(5), "never released");
        tokio::time::sleep(Duration::from_millis(20)).await;
    };
    assert_eq!(outcome(&items), "result Success");
    db.finish().await;
}

/// The holder itself resumes a run it dropped at once: its claim never
/// waits on its own lease.
#[tokio::test]
async fn the_holder_resumes_its_own_dropped_run_at_once() {
    let Some(db) = TestDb::migrated("lease_own").await else {
        return;
    };
    let a = Process::new(&db, "from a", 2, None).await;
    let stream_a = at_the_gate(&a).await;
    let run_id = stream_a.run_id().clone();
    drop(stream_a);
    a.open();
    let items = a.resume(&run_id).await;
    assert_eq!(outcome(&items), "result Success");
    assert_eq!(record(&db, &run_id).await, (RunStatus::Completed, 2));
    db.finish().await;
}
