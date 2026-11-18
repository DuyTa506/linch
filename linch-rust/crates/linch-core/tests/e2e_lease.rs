//! The run lease heartbeat, on a memory store that reports a TTL and
//! counts renewals and releases: renewals come on a timer while a model
//! call is in flight, also while nobody polls the stream, and one that hangs
//! costs one beat; a run that ends releases nothing, and a dropped run
//! releases its lease once.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Commit, Envelope, Event, JournalError, Lease, MemoryRunStore,
    MemorySessionStore, NewRun, PermissionEngine, PermissionMode, PermissionPolicy, Provider,
    ProviderError, ProviderRequest, ProviderStream, RunError, RunId, RunOptions, RunRecord,
    RunStore, ScriptedProvider, StoreError,
};
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;

const MODEL: &str = "scripted-model";
const TTL: Duration = Duration::from_millis(150);

/// A memory run store whose claims nominally expire after [`TTL`].
#[derive(Default)]
struct Leasing {
    inner: MemoryRunStore,
    renewals: AtomicU64,
    releases: AtomicU64,
    /// Renewals still to hang for good before they answer.
    hangs: AtomicU64,
    /// Renewals that began, hung ones included.
    attempts: AtomicU64,
    /// Whether a renewal panics.
    panics: AtomicBool,
}

#[async_trait]
impl RunStore for Leasing {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.inner.create(run).await
    }

    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.inner.claim(ns, run_id, owner).await
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

    fn lease_ttl(&self) -> Option<Duration> {
        Some(TTL)
    }

    async fn renew(&self, _lease: &Lease) -> Result<(), StoreError> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        assert!(!self.panics.load(Ordering::SeqCst), "the store panicked");
        let hangs = self.hangs.load(Ordering::SeqCst);
        if hangs > 0 {
            self.hangs.store(hangs - 1, Ordering::SeqCst);
            std::future::pending::<()>().await;
        }
        self.renewals.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    async fn release(&self, _lease: &Lease) -> Result<(), StoreError> {
        self.releases.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

/// A scripted provider whose calls wait until the test opens the gate.
struct Gated {
    inner: ScriptedProvider,
    entered: Notify,
    open: Semaphore,
}

#[async_trait]
impl Provider for Gated {
    fn id(&self) -> &str {
        "gated"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.entered.notify_one();
        self.open.acquire().await.expect("never closed").forget();
        self.inner.stream(request, cancel).await
    }
}

fn setup() -> (Arc<Leasing>, Arc<Gated>, linch_core::Agent) {
    let runs = Arc::new(Leasing::default());
    let provider = Arc::new(Gated {
        inner: ScriptedProvider::new([TurnBuilder::new(MODEL).text("done").build()]),
        entered: Notify::new(),
        open: Semaphore::new(0),
    });
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(Arc::clone(&provider) as Arc<dyn Provider>)
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::clone(&runs) as Arc<dyn RunStore>)
        .session_store(Arc::new(MemorySessionStore::default()))
        .namespace("ns")
        .build()
        .unwrap();
    (runs, provider, agent)
}

#[tokio::test]
async fn a_long_model_call_renews_on_a_timer_and_an_ended_run_releases_nothing() {
    let (runs, provider, agent) = setup();
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let (entered, drained) = (provider.entered.notified(), async {
        let mut last = None;
        while let Some(item) = stream.next().await {
            last = Some(item.expect("the run completes"));
        }
        last
    });
    let release_after_ten_beats = async {
        entered.await;
        tokio::time::sleep(TTL / 3 * 10 + TTL / 6).await;
        provider.open.add_permits(1);
    };
    let (last, ()) = tokio::join!(drained, release_after_ten_beats);
    assert!(matches!(last.unwrap().event, Event::Result(_)));
    let renewals = runs.renewals.load(Ordering::SeqCst);
    assert!(
        (5..=12).contains(&renewals),
        "about one per TTL/3: {renewals}"
    );
    assert_eq!(runs.releases.load(Ordering::SeqCst), 0, "an ended run");
}

#[tokio::test]
async fn a_dropped_run_releases_its_lease_once() {
    let (runs, provider, agent) = setup();
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let polled = async { while stream.next().await.is_some() {} };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = provider.entered.notified() => {}
    }
    drop(stream);
    for _ in 0..100 {
        if runs.releases.load(Ordering::SeqCst) > 0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(runs.releases.load(Ordering::SeqCst), 1);
}

/// A stream nobody polls (a consumer that stopped reading) keeps its lease:
/// the heartbeat is a task of the run, not a part of what the poll drives.
#[tokio::test]
async fn an_unpolled_stream_keeps_renewing() {
    let (runs, provider, agent) = setup();
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let polled = async { while stream.next().await.is_some() {} };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = provider.entered.notified() => {}
    }
    tokio::time::sleep(TTL / 3 * 10 + TTL / 6).await;
    let renewals = runs.renewals.load(Ordering::SeqCst);
    assert!(
        (5..=12).contains(&renewals),
        "about one per TTL/3 with nobody polling: {renewals}"
    );
    // The run is still whole: it finishes once the gate opens.
    provider.open.add_permits(1);
    let items: Vec<_> = stream.collect().await;
    let last = items.last().expect("the run ends");
    assert!(matches!(last.as_ref().unwrap().event, Event::Result(_)));
}

/// A renewal that never answers costs the beat it takes, not the heartbeat:
/// the next beat does not wait for it.
#[tokio::test]
async fn a_renewal_that_hangs_does_not_stop_the_beat() {
    let (runs, provider, agent) = setup();
    runs.hangs.store(2, Ordering::SeqCst);
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let polled = async { while stream.next().await.is_some() {} };
    tokio::select! {
        () = polled => panic!("the gate holds the run"),
        () = provider.entered.notified() => {}
    }
    tokio::time::sleep(TTL / 3 * 10 + TTL / 6).await;
    let (attempts, renewals) = (
        runs.attempts.load(Ordering::SeqCst),
        runs.renewals.load(Ordering::SeqCst),
    );
    assert!(attempts >= 5 && renewals >= 3, "{attempts} {renewals}");
}

/// A heartbeat that dies (its store panicked) cannot keep the lease: the run
/// ends with `LeaseLost` instead of running on unprotected.
#[tokio::test]
async fn a_heartbeat_that_panics_ends_the_run_with_lease_lost() {
    let (runs, provider, agent) = setup();
    runs.panics.store(true, Ordering::SeqCst);
    let mut stream = agent.run("hi", RunOptions::new("s1"));
    let mut last = None;
    let drained = async {
        while let Some(item) = stream.next().await {
            last = Some(item);
        }
    };
    tokio::time::timeout(TTL * 3, drained)
        .await
        .expect("the run ends without the gate opening");
    let ended = matches!(
        last,
        Some(Err(RunError::Journal(JournalError::Store(
            StoreError::LeaseLost { .. }
        ))))
    );
    assert!(ended, "{last:?}");
    drop(provider);
}
