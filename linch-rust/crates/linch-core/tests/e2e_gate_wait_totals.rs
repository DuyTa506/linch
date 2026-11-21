//! A run that writes the events it staged so far before a call, because it
//! waits for a place in the provider gate or because a steer arrived, must
//! still count every receipt among them in its totals when the process stops
//! right after and the run is resumed.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use common::flaky::Flaky;
use common::hooks::hook;
use common::*;
use linch_core::events::kinds::ResultEvent;
use linch_core::events::model_call::{CallDisposition, ModelCallReceiptEvent};
use linch_core::hooks::{BeforeProviderCall, BeforeProviderCallResponse};
use linch_core::provider::scripted::{ScriptedError, TurnBuilder};
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, Event, Hooks, PermissionMode, Phase, Provider,
    ProviderGate, ProviderPermit, ProviderRetry, RunId, RunOptions, RunStore, SessionStore,
    ToolScope, Usage,
};

const FALLBACK: &str = "fallback-a";

fn price(_model: &str, usage: &Usage) -> Option<f64> {
    Some(usage.input_tokens as f64 * 0.001 + usage.output_tokens as f64 * 0.002)
}

fn gated<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    hooks: Hooks,
    gate: &ProviderGate,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new(
            "Read",
            ToolScope::Read,
            &Runs::default(),
        )]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .pricing(price)
        .provider_gate(gate.clone())
        .build()
        .unwrap()
}

fn gate() -> ProviderGate {
    ProviderGate::new(std::num::NonZeroU32::new(1).unwrap())
}

/// The stored checkpoint is still in the phase of the failed or abandoned
/// call (attempt 1): only its totals moved.
async fn assert_phase_kept(stores: &Stores, run_id: &RunId) {
    let checkpoint = stores.runs.load(NS, run_id).await.unwrap().checkpoint;
    let phase = checkpoint.phase;
    assert!(
        matches!(phase, Phase::ProviderPending { attempt: 1 }),
        "{phase:?}"
    );
}

async fn durable(stores: &Stores, run_id: &RunId) -> Vec<Envelope> {
    stores.runs.events(NS, run_id, 0).await.unwrap()
}

fn receipts(events: &[Envelope]) -> Vec<ModelCallReceiptEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt.clone()),
            _ => None,
        })
        .collect()
}

fn result(events: &[Envelope]) -> ResultEvent {
    match events.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result.clone(),
        other => panic!("the run has no result: {other:?}"),
    }
}

/// Wait until the run's durable log holds `count` receipts, for at most 5 s.
async fn until_receipts(stores: &Stores, run_id: &RunId, count: usize) {
    for _ in 0..500 {
        // The run may not be created yet.
        let events = stores.runs.events(NS, run_id, 0).await;
        if receipts(&events.unwrap_or_default()).len() == count {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the receipt never became durable");
}

/// Resume `run_id` while the gate is full, wait until the resumed run wrote
/// `count` receipts before its wait, then stop it there, as a kill would.
async fn stop_at_the_gate(
    agent: &Agent,
    gate: &ProviderGate,
    stores: &Stores,
    run_id: &RunId,
    count: usize,
) {
    let place = gate.try_acquire().expect("the gate is free");
    let stream = agent.resume(run_id.clone(), RunOptions::new("s1"));
    let waiting = tokio::spawn(drain(stream));
    until_receipts(stores, run_id, count).await;
    waiting.abort();
    let _ = waiting.await;
    drop(place);
}

/// A run stopped in its provider call (the delivery is durable, no receipt).
async fn stopped_in_the_call(gate: &ProviderGate, stores: &Arc<Stores>) -> RunId {
    // Writes: session create, run create, claim, prompt, provider pending.
    let faulty = Arc::new(Faulty::new(stores.clone(), 5, FaultTiming::After));
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = gated(spec(), provider, Hooks::default(), gate, faulty);
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    assert!(drain(stream).await.1.is_some(), "the run must stop");
    let kinds = kinds(&durable(stores, &run_id).await);
    assert_eq!(kinds.last().unwrap(), "model_call_delivery", "{kinds:?}");
    run_id
}

/// The control: the abandoned call has no usage, so the resumed run's
/// result says its usage is incomplete.
#[tokio::test]
async fn an_abandoned_call_makes_the_resumed_usage_incomplete() {
    let (gate, stores) = (gate(), Arc::new(Stores::default()));
    let run_id = stopped_in_the_call(&gate, &stores).await;
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = gated(spec(), provider, Hooks::default(), &gate, stores.clone());
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    let events = durable(&stores, &run_id).await;
    let receipts = receipts(&events);
    assert_eq!(receipts.len(), 2);
    assert_eq!(receipts[0].disposition, CallDisposition::Cancelled);
    assert!(!result(&events).usage_complete);
}

/// The same run, stopped once more while its resume waits for the gate:
/// the abandoned receipt is durable, and the result must still count it.
#[tokio::test]
async fn a_stop_while_waiting_for_the_gate_keeps_the_usage_incomplete() {
    let (gate, stores) = (gate(), Arc::new(Stores::default()));
    let run_id = stopped_in_the_call(&gate, &stores).await;
    let provider = Arc::new(Replay::new(vec![text_turn("done")]));
    let agent = gated(spec(), provider, Hooks::default(), &gate, stores.clone());
    stop_at_the_gate(&agent, &gate, &stores, &run_id, 1).await;
    assert_phase_kept(&stores, &run_id).await;
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    let events = durable(&stores, &run_id).await;
    let receipts = receipts(&events);
    assert_eq!(receipts.len(), 2, "{:?}", kinds(&events));
    assert_eq!(receipts[0].disposition, CallDisposition::Cancelled);
    assert!(receipts[0].usage.is_none());
    assert!(
        !result(&events).usage_complete,
        "a receipt without usage is durable, but the result says the usage is complete"
    );
}

fn failover_spec() -> AgentSpec {
    let mut spec = spec();
    spec.fallback_models = vec![FALLBACK.into()];
    spec.provider_retry = ProviderRetry {
        max_attempts: 1,
        base_delay_ms: 1,
        max_delay_ms: 1,
        jitter: 0.0,
    };
    spec
}

/// The spec's model breaks once after it reported its usage (a paid
/// failure); the fallback answers.
fn paid_failure() -> Arc<Flaky> {
    paid_failure_then(vec![text_turn("done")])
}

/// [`paid_failure`], then the fallback answers with `turns`.
fn paid_failure_then(turns: Vec<linch_core::ScriptedTurn>) -> Arc<Flaky> {
    let mut paid = TurnBuilder::new(MODEL)
        .text("partial")
        .usage(usage(7, 3))
        .build();
    paid.error = Some(ScriptedError::Transport {
        message: "reset".into(),
    });
    Arc::new(Flaky::new(turns).fail(0, MODEL, paid))
}

/// A `before_provider_call` hook that takes the gate's place before the
/// second call of the run, so that call waits; `None` takes nothing.
fn filling(gate: Option<ProviderGate>, held: Arc<Mutex<Option<ProviderPermit>>>) -> Hooks {
    let hooks = Hooks::default();
    let calls = Mutex::new(0);
    let fill = hook::<BeforeProviderCall>("fill", move |_| {
        let mut calls = calls.lock().unwrap();
        *calls += 1;
        if let (2, Some(gate)) = (*calls, &gate) {
            *held.lock().unwrap() = gate.try_acquire();
        }
        Ok(BeforeProviderCallResponse::Continue)
    });
    let _ = hooks.before_provider_call.register(fill.arc()).unwrap();
    hooks
}

/// The totals of the result must be the sum of the durable receipts.
fn assert_totals(events: &[Envelope]) {
    let receipts = receipts(events);
    let mut usage = Usage::default();
    for known in receipts.iter().filter_map(|r| r.usage) {
        usage += known;
    }
    let cost: f64 = receipts.iter().filter_map(|r| r.cost_usd).sum();
    let result = result(events);
    assert_eq!(result.total_usage, usage, "total_usage");
    let total = result.total_cost_usd.unwrap_or(0.0);
    assert!((total - cost).abs() < 1e-9, "total cost {total} != {cost}");
}

/// The control: a paid failure, a failover, no stop.
#[tokio::test]
async fn a_failed_over_run_counts_its_paid_failure() {
    let (gate, stores) = (gate(), Arc::new(Stores::default()));
    let hooks = filling(None, Arc::default());
    let agent = gated(
        failover_spec(),
        paid_failure(),
        hooks,
        &gate,
        stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;
    let events = durable(&stores, &run_id).await;
    assert_eq!(receipts(&events).len(), 2);
    assert_totals(&events);
}

/// The failover's call waits for the gate, which writes the paid failure's
/// receipt; the process stops there. The resumed run's result must count
/// that receipt.
#[tokio::test]
async fn a_stop_while_a_failover_waits_for_the_gate_keeps_the_paid_failure() {
    let (gate, stores) = (gate(), Arc::new(Stores::default()));
    let held = Arc::new(Mutex::new(None));
    let hooks = filling(Some(gate.clone()), held.clone());
    let provider = paid_failure();
    let agent = gated(
        failover_spec(),
        provider.clone(),
        hooks,
        &gate,
        stores.clone(),
    );
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let waiting = tokio::spawn(drain(stream));
    until_receipts(&stores, &run_id, 1).await;
    waiting.abort();
    let _ = waiting.await;
    held.lock().unwrap().take();
    assert_phase_kept(&stores, &run_id).await;
    let failed = receipts(&durable(&stores, &run_id).await);
    assert_eq!(failed[0].usage, Some(usage(7, 3)), "the failure was paid");

    let hooks = filling(None, Arc::default());
    let agent = gated(failover_spec(), provider, hooks, &gate, stores.clone());
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
    let events = durable(&stores, &run_id).await;
    assert_eq!(receipts(&events).len(), 2, "{:?}", kinds(&events));
    assert_totals(&events);
}

/// A provider that steers the run `r1` during its first call, as a host
/// would meanwhile, then answers as `inner` does.
struct SteerFirst {
    inner: Arc<Flaky>,
    agent: Arc<OnceLock<Agent>>,
    steered: AtomicBool,
}

#[async_trait::async_trait]
impl Provider for SteerFirst {
    fn id(&self) -> &str {
        "steer-first"
    }
    async fn stream(
        &self,
        request: linch_core::ProviderRequest,
        cancel: tokio_util::sync::CancellationToken,
    ) -> Result<linch_core::ProviderStream, linch_core::ProviderError> {
        if !self.steered.swap(true, Ordering::SeqCst) {
            let agent = self.agent.get().expect("the agent is set").clone();
            tokio::spawn(async move {
                let id = Some("steer-1".to_owned());
                agent.steer(RunId::new("r1"), "use tabs", id).await
            });
            tokio::task::yield_now().await;
        }
        self.inner.stream(request, cancel).await
    }
}

fn first_options() -> RunOptions {
    let mut options = RunOptions::new("s1");
    options.run_id = Some(RunId::new("r1"));
    options
}

/// The run of `paid_failure` and a failover, which a steer interrupts
/// during the failed call, on `stores` for both run and session storage.
async fn steered_failover<S: RunStore + SessionStore + 'static>(stores: Arc<S>) {
    let cell = Arc::new(OnceLock::new());
    // The steer makes a turn of its own after the answer.
    let turns = vec![text_turn("done"), text_turn("done")];
    let provider = Arc::new(SteerFirst {
        inner: paid_failure_then(turns),
        agent: cell.clone(),
        steered: AtomicBool::new(false),
    });
    let hooks = filling(None, Arc::default());
    let agent = gated(failover_spec(), provider, hooks, &gate(), stores);
    cell.set(agent.clone()).ok().expect("set once");
    let stream = agent.run("go", first_options());
    let _ = drain(stream).await;
}

/// A steer that arrives while a call is in flight is written with the
/// events staged since, which hold the failed call's receipt. The process
/// stops right after that write; the resumed run's result must count it.
#[tokio::test]
async fn a_stop_right_after_a_steer_is_written_keeps_the_paid_failure() {
    // Where the write that holds the steer sits in the order of all writes.
    let counted = Arc::new(Stores::default());
    steered_failover(counted.clone()).await;
    let log = counted.log.lock().unwrap().clone();
    let steer_write = log.iter().position(|write| {
        matches!(write, Write::Commit(kinds) if kinds.iter().any(|kind| kind == "steer_received"))
    });
    let steer_write = steer_write.expect("a write holds the steer") as u64 + 1;
    let events = counted.runs.events(NS, &RunId::new("r1"), 0).await.unwrap();
    assert_eq!(receipts(&events).len(), 3, "{:?}", kinds(&events));

    let stores = Arc::new(Stores::default());
    let faulty = Arc::new(Faulty::new(stores.clone(), steer_write, FaultTiming::After));
    steered_failover(faulty).await;
    let landed = durable(&stores, &RunId::new("r1")).await;
    assert_eq!(kinds(&landed).last().unwrap(), "steer_received");
    assert_eq!(receipts(&landed).len(), 1, "{:?}", kinds(&landed));
    assert_phase_kept(&stores, &RunId::new("r1")).await;

    let hooks = filling(None, Arc::default());
    let turns = vec![text_turn("done"), text_turn("done")];
    let provider = paid_failure_then(turns);
    let agent = gated(failover_spec(), provider, hooks, &gate(), stores.clone());
    finish(agent.resume(RunId::new("r1"), RunOptions::new("s1"))).await;
    let events = durable(&stores, &RunId::new("r1")).await;
    assert_totals(&events);
}
