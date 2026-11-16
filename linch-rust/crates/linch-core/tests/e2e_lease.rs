//! The run lease heartbeat, on a memory store that reports a TTL and
//! counts renewals and releases: renewals come on a timer while a model
//! call is in flight, a run that ends releases nothing, and a dropped run
//! releases its lease once.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, AgentSpec, Commit, Envelope, Event, Lease, MemoryRunStore, MemorySessionStore,
    NewRun, PermissionEngine, PermissionMode, PermissionPolicy, Provider, ProviderError,
    ProviderRequest, ProviderStream, RunId, RunOptions, RunRecord, RunStore, ScriptedProvider,
    StoreError,
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
        (8..=12).contains(&renewals),
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
