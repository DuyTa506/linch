//! The provider gate: at most its cap of provider calls run at once across
//! the runs of an agent and its subagents; a run that waits for a place has
//! staged nothing, so cancelling it leaves no delivery or receipt; and an
//! agent without a gate, or with one, has the same contract.

mod common;

use std::num::NonZeroU32;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::ResultSubtype;
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, PermissionMode, Provider, ProviderError, ProviderGate,
    ProviderRequest, ProviderStream, RunId, RunOptions, RunStatus, RunStore, ToolScope,
};
use tokio::sync::Semaphore;
use tokio_util::sync::CancellationToken;

/// Answers `done`, but only once the test lets it: each call takes one of
/// `go`'s permits. Counts the calls now in flight and the most there were.
struct Held {
    go: Semaphore,
    running: AtomicUsize,
    peak: AtomicUsize,
    calls: AtomicUsize,
}

impl Held {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            go: Semaphore::new(0),
            running: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
            calls: AtomicUsize::new(0),
        })
    }

    fn running(&self) -> usize {
        self.running.load(Ordering::SeqCst)
    }

    fn peak(&self) -> usize {
        self.peak.load(Ordering::SeqCst)
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl Provider for Held {
    fn id(&self) -> &str {
        "held"
    }

    async fn stream(
        &self,
        _request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let now = self.running.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(now, Ordering::SeqCst);
        self.go.acquire().await.unwrap().forget();
        self.running.fetch_sub(1, Ordering::SeqCst);
        let events: Vec<_> = text_turn("done").events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

fn gate(max: u32) -> ProviderGate {
    ProviderGate::new(NonZeroU32::new(max).unwrap())
}

fn gated(provider: Arc<Held>, gate: Option<ProviderGate>, stores: Arc<Stores>) -> Agent {
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
    let mut builder = AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS);
    if let Some(gate) = gate {
        builder = builder.provider_gate(gate);
    }
    builder.build().unwrap()
}

/// Wait until `check` holds, for at most 5 s.
async fn until(check: impl Fn() -> bool) {
    for _ in 0..500 {
        if check() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the condition never held");
}

/// A pause long enough for a call that is allowed to start to start.
async fn settle() {
    tokio::time::sleep(Duration::from_millis(100)).await;
}

fn start(agent: &Agent, session: &str, cancel: CancellationToken) -> (RunId, RunHandle) {
    let mut options = RunOptions::new(session);
    options.cancel = cancel;
    let stream = agent.run("go", options);
    let run_id = stream.run_id().clone();
    (run_id, tokio::spawn(async move { drain(stream).await.0 }))
}

type RunHandle = tokio::task::JoinHandle<Vec<Envelope>>;

fn subtype(envelopes: &[Envelope]) -> ResultSubtype {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result.subtype,
        other => panic!("the run has no result: {other:?}"),
    }
}

/// Five runs through a gate of two: exactly two calls run at once, never
/// more, and every run completes.
#[tokio::test]
async fn at_most_the_cap_of_calls_run_at_once() {
    let provider = Held::new();
    let stores = Arc::new(Stores::default());
    let gate = gate(2);
    let agent = gated(provider.clone(), Some(gate.clone()), stores);
    let runs: Vec<_> = (0..5)
        .map(|n| start(&agent, &format!("s{n}"), CancellationToken::new()).1)
        .collect();

    until(|| provider.running() == 2).await;
    settle().await;
    assert_eq!(provider.running(), 2, "two calls fill the gate");
    assert_eq!(provider.peak(), 2, "and no third got in");
    assert_eq!(gate.in_flight(), 2);

    provider.go.add_permits(5);
    for run in runs {
        assert_eq!(subtype(&run.await.unwrap()), ResultSubtype::Success);
    }
    assert_eq!((provider.calls(), provider.peak()), (5, 2));
    assert_eq!(gate.in_flight(), 0, "every place is given back");
}

/// Without a gate the same five runs all call at once.
#[tokio::test]
async fn without_a_gate_every_call_runs() {
    let provider = Held::new();
    let agent = gated(provider.clone(), None, Arc::new(Stores::default()));
    let runs: Vec<_> = (0..5)
        .map(|n| start(&agent, &format!("s{n}"), CancellationToken::new()).1)
        .collect();
    until(|| provider.running() == 5).await;
    provider.go.add_permits(5);
    for run in runs {
        run.await.unwrap();
    }
    assert_eq!(provider.peak(), 5);
}

/// A run cancelled while it waits for a place aborts with no delivery, no
/// receipt and no provider call; it takes no place, so a run that asks
/// later still gets the one the first run frees.
#[tokio::test]
async fn cancelling_a_run_that_waits_leaves_no_call_and_no_receipt() {
    let provider = Held::new();
    let stores = Arc::new(Stores::default());
    let gate = gate(1);
    let agent = gated(provider.clone(), Some(gate.clone()), stores.clone());
    let (_, first) = start(&agent, "s1", CancellationToken::new());
    until(|| provider.running() == 1).await;

    let cancel = CancellationToken::new();
    let (waiting, second) = start(&agent, "s2", cancel.clone());
    settle().await;
    assert_eq!(provider.calls(), 1, "the second run waits for its place");
    cancel.cancel();
    let envelopes = second.await.unwrap();
    assert_eq!(subtype(&envelopes), ResultSubtype::Aborted);

    let events = stores.runs.events(NS, &waiting, 0).await.unwrap();
    let kinds = kinds(&events);
    assert!(
        !kinds.iter().any(|k| k.starts_with("model_call")),
        "nothing of the call was staged: {kinds:?}"
    );
    assert_eq!(
        stores.runs.load(NS, &waiting).await.unwrap().status,
        RunStatus::Aborted
    );
    assert_eq!(provider.calls(), 1, "the call never went out");

    let (_, third) = start(&agent, "s3", CancellationToken::new());
    settle().await;
    provider.go.add_permits(2);
    assert_eq!(subtype(&first.await.unwrap()), ResultSubtype::Success);
    assert_eq!(subtype(&third.await.unwrap()), ResultSubtype::Success);
    assert_eq!(provider.calls(), 2, "the cancelled run left the queue");
    assert_eq!(gate.in_flight(), 0);
}

/// A run that waits has its earlier staged events durable first: it is not
/// a run whose paid receipt is held in memory behind another run's call.
#[tokio::test]
async fn a_run_that_waits_has_its_session_and_start_durable() {
    let provider = Held::new();
    let stores = Arc::new(Stores::default());
    let agent = gated(provider.clone(), Some(gate(1)), stores.clone());
    let (_, first) = start(&agent, "s1", CancellationToken::new());
    until(|| provider.running() == 1).await;
    let (waiting, second) = start(&agent, "s2", CancellationToken::new());
    settle().await;
    let events = stores.runs.events(NS, &waiting, 0).await.unwrap();
    assert!(
        kinds(&events).contains(&"init".to_owned()),
        "its start is durable while it waits: {:?}",
        kinds(&events)
    );
    provider.go.add_permits(2);
    first.await.unwrap();
    second.await.unwrap();
}

/// The gate is not part of the contract: an agent with one has the digest
/// of one without, so changing the cap never strands a suspended run.
#[tokio::test]
async fn the_gate_is_not_part_of_the_contract() {
    let stores = Arc::new(Stores::default());
    let bare = gated(Held::new(), None, stores.clone());
    let capped = gated(Held::new(), Some(gate(3)), stores.clone());
    let other = gated(Held::new(), Some(gate(7)), stores);
    assert_eq!(bare.contract(), capped.contract());
    assert_eq!(bare.contract(), other.contract());
}
