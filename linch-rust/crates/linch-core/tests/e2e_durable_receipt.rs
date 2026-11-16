//! A paid receipt is durable before the hooks that follow its call run.
//!
//! With an `after_provider_call` (or `provider_call_stop`) chain attached,
//! the accepted receipt and its usage are committed as soon as the stream
//! settles, before the hooks, so a crash while a hook runs keeps the paid
//! usage. Resume finds the accepted receipt with no `assistant` event (an
//! orphan), keeps it in the totals, and dispatches the call again under the
//! next delivery attempt, without spending a provider retry. Without such
//! a chain the run writes exactly as before.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::flaky::{Flaky, busy};
use common::hooks::hook;
use common::*;
use linch_core::events::kinds::{BudgetKind, ResultEvent, ResultSubtype};
use linch_core::events::model_call::{CallDisposition, ModelCallKind, ModelCallReceiptEvent};
use linch_core::hooks::{AfterProviderCall, AfterProviderCallRequest, AfterProviderCallResponse};
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::run::Phase;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, CallbackError, Commit, ContentBlock, Envelope, Event, Hook,
    Hooks, Lease, LogEntry, Message, NewRun, PermissionMode, Provider, ProviderRetry, RunBudget,
    RunId, RunOptions, RunRecord, RunStatus, RunStore, SessionId, SessionStore, StoreError,
    ToolResultBlock, ToolScope, Usage,
};
use tokio::sync::Notify;

/// A thousandth of a USD per input token, two per output token: 0.02 for
/// one scripted call.
fn price(_model: &str, usage: &Usage) -> Option<f64> {
    Some(usage.input_tokens as f64 * 0.001 + usage.output_tokens as f64 * 0.002)
}

fn turns() -> Vec<linch_core::ScriptedTurn> {
    vec![tool_turn(0, 1, "Read"), text_turn("done")]
}

fn build<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    hooks: Hooks,
    stores: Arc<S>,
    runs: &Runs,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .pricing(price)
        .build()
        .unwrap()
}

/// An `after_provider_call` chain that accepts every response.
fn accepting() -> Hooks {
    let hooks = Hooks::default();
    let judge = hook::<AfterProviderCall>("judge", |_| Ok(AfterProviderCallResponse::Continue));
    let _ = hooks.after_provider_call.register(judge.arc()).unwrap();
    hooks
}

/// The same policy as [`accepting`], but its call never returns, like a
/// client callback with no timeout on a host that is about to crash.
struct Stall {
    entered: Arc<Notify>,
}

#[async_trait]
impl Hook<AfterProviderCall> for Stall {
    fn policy_id(&self) -> &str {
        "judge"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        _request: &AfterProviderCallRequest,
    ) -> Result<AfterProviderCallResponse, CallbackError> {
        self.entered.notify_one();
        std::future::pending().await
    }
}

/// Start a run whose first `after_provider_call` never returns, and drop
/// it there, as a crash would.
async fn crash_in_hook(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    stores: Arc<Stores>,
    runs: &Runs,
) -> RunId {
    let entered = Arc::new(Notify::new());
    let hooks = Hooks::default();
    let stall = Stall {
        entered: Arc::clone(&entered),
    };
    let _ = hooks.after_provider_call.register(Arc::new(stall)).unwrap();
    let stream = build(spec, provider, hooks, stores, runs).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    tokio::select! {
        _ = drain(stream) => panic!("the run ended inside a hook that never returns"),
        () = entered.notified() => {}
    }
    run_id
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

/// The run's totals must be the sum of its receipts, orphans included.
fn assert_totals(events: &[Envelope], context: &str) {
    let receipts = receipts(events);
    let mut usage = Usage::default();
    for known in receipts.iter().filter_map(|r| r.usage) {
        usage += known;
    }
    let cost: f64 = receipts.iter().filter_map(|r| r.cost_usd).sum();
    let result = result(events);
    assert_eq!(result.total_usage, usage, "{context}");
    assert_eq!(
        result.usage_complete,
        receipts.iter().all(|r| r.usage.is_some()),
        "{context}"
    );
    let total = result.total_cost_usd.unwrap_or(0.0);
    assert!((total - cost).abs() < 1e-9, "{context}: {total} != {cost}");
    // The event stream shows every known usage, the orphan's included.
    let mut shown = Usage::default();
    for envelope in events {
        if let Event::Usage(event) = &envelope.event {
            shown += event.usage;
        }
    }
    assert_eq!(shown, usage, "{context}: usage events");
}

/// Turn index of a model-call event, from its logical id `{run}:primary:{turn}`.
fn turn_of(logical_call_id: &str) -> usize {
    logical_call_id.rsplit(':').next().unwrap().parse().unwrap()
}

/// Per turn, the accepted primary receipts that an `assistant` event
/// answered, and the orphans: accepted receipts with no `assistant` event
/// before the next delivery.
fn accepted(events: &[Envelope]) -> HashMap<usize, (u32, u32)> {
    let mut counts: HashMap<usize, (u32, u32)> = HashMap::new();
    let mut open = None;
    let orphan = |turn: Option<usize>, counts: &mut HashMap<usize, (u32, u32)>| {
        if let Some(turn) = turn {
            counts.entry(turn).or_default().1 += 1;
        }
    };
    for envelope in events {
        match &envelope.event {
            Event::ModelCallReceipt(r)
                if r.kind == ModelCallKind::Primary
                    && r.disposition == CallDisposition::Accepted =>
            {
                orphan(open.take(), &mut counts);
                open = Some(turn_of(&r.logical_call_id));
            }
            Event::Assistant(_) => {
                if let Some(turn) = open.take() {
                    counts.entry(turn).or_default().0 += 1;
                }
            }
            Event::ModelCallDelivery(_) => orphan(open.take(), &mut counts),
            _ => {}
        }
    }
    orphan(open, &mut counts);
    counts
}

/// While `after_provider_call` runs, the paid call's accepted receipt and
/// its usage are already durable, under a checkpoint whose totals count
/// them; the response itself is not.
#[tokio::test]
async fn a_paid_response_is_durable_while_after_provider_call_runs() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Replay::new(turns()));
    let run_id = crash_in_hook(spec(), provider, stores.clone(), &Runs::default()).await;

    let events = durable(&stores, &run_id).await;
    let receipts = receipts(&events);
    assert_eq!(receipts.len(), 1, "{receipts:?}");
    assert_eq!(receipts[0].disposition, CallDisposition::Accepted);
    assert_eq!(
        receipts[0].delivery_attempt_id,
        format!("{run_id}:primary:0:1")
    );
    assert_eq!(receipts[0].usage, Some(usage(10, 5)));
    assert!(kinds(&events).contains(&"usage".to_owned()));
    assert!(
        !kinds(&events).contains(&"assistant".to_owned()),
        "the response waits for its hooks"
    );
    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Running);
    assert_eq!(
        record.checkpoint.phase,
        Phase::ProviderPending { attempt: 1 }
    );
    assert_eq!(record.checkpoint.usage, usage(10, 5));
    assert_eq!(record.checkpoint.cost_usd, Some(0.02));
}

/// A resume after that crash keeps the orphan in the totals and asks the
/// provider again under the next delivery attempt; the run ends as a clean
/// one does, with no provider retry spent and no tool run twice.
#[tokio::test]
async fn a_resume_keeps_the_orphan_and_calls_again() {
    let clean = Arc::new(Stores::default());
    let agent = build(
        spec(),
        Arc::new(Replay::new(turns())),
        accepting(),
        clean.clone(),
        &Runs::default(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;

    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let provider = Arc::new(Replay::new(turns()));
    let run_id = crash_in_hook(spec(), provider.clone(), stores.clone(), &runs).await;
    let agent = build(spec(), provider.clone(), accepting(), stores.clone(), &runs);
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    assert_eq!(messages(&*stores, "s1").await, want);
    assert!(runs.lock().unwrap().values().all(|&n| n == 1), "{runs:?}");
    assert_eq!(provider.calls(), HashMap::from([(0, 2), (1, 1)]));
    let events = durable(&stores, &run_id).await;
    let ids: Vec<_> = (receipts(&events).into_iter())
        .map(|r| (r.delivery_attempt_id, r.disposition))
        .collect();
    let accepted_as = |id: &str| (format!("{run_id}:primary:{id}"), CallDisposition::Accepted);
    assert_eq!(
        ids,
        [accepted_as("0:1"), accepted_as("0:2"), accepted_as("1:1")]
    );
    assert_eq!(accepted(&events), HashMap::from([(0, (1, 1)), (1, (1, 0))]));
    assert!(!kinds(&events).contains(&"provider_retry".to_owned()));
    assert_totals(&events, "resumed");
    let total = result(&events).total_cost_usd.unwrap();
    assert!((total - 0.06).abs() < 1e-9, "three paid calls: {total}");
}

/// The call after an orphan is not a retry: with one retry allowed, it can
/// still fail once and be retried.
#[tokio::test]
async fn an_orphan_spends_no_provider_retry() {
    let mut spec = spec();
    spec.provider_retry = ProviderRetry {
        max_attempts: 2,
        base_delay_ms: 1,
        max_delay_ms: 1,
        jitter: 0.0,
    };
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let first = Arc::new(Replay::new(turns()));
    let run_id = crash_in_hook(spec.clone(), first, stores.clone(), &runs).await;
    let flaky = Arc::new(Flaky::new(turns()).fail_with(0, MODEL, busy()));
    let agent = build(spec, flaky, accepting(), stores.clone(), &runs);
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Completed);
    let events = durable(&stores, &run_id).await;
    let retries: Vec<u32> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::ProviderRetry(retry) => Some(retry.retry),
            _ => None,
        })
        .collect();
    assert_eq!(retries, [1]);
    let turn0: Vec<_> = (receipts(&events).into_iter())
        .filter(|r| turn_of(&r.logical_call_id) == 0)
        .map(|r| (r.delivery_attempt_id, r.disposition))
        .collect();
    let id = |attempt: u32| format!("{run_id}:primary:0:{attempt}");
    assert_eq!(
        turn0,
        [
            (id(1), CallDisposition::Accepted),
            (id(2), CallDisposition::Failed),
            (id(3), CallDisposition::Accepted),
        ]
    );
    assert_totals(&events, "retried");
}

/// The orphan is spent money: under a capped budget it can leave no room
/// for the call again, and the resumed run ends on the budget.
#[tokio::test]
async fn an_orphan_can_end_a_capped_run_on_its_budget() {
    let mut spec = spec();
    spec.budget = Some(RunBudget {
        max_tokens: None,
        max_cost_usd: Some(0.02),
        warn_ratio: 0.8,
    });
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let provider = Arc::new(Replay::new(turns()));
    let run_id = crash_in_hook(spec.clone(), provider.clone(), stores.clone(), &runs).await;
    let agent = build(spec, provider.clone(), accepting(), stores.clone(), &runs);
    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;

    let record = stores.runs.load(NS, &run_id).await.unwrap();
    assert_eq!(record.status, RunStatus::Failed);
    assert_eq!(provider.calls(), HashMap::from([(0, 1)]), "no call again");
    assert!(runs.lock().unwrap().is_empty(), "no tool ran");
    let events = durable(&stores, &run_id).await;
    let exceeded = events
        .iter()
        .any(|e| matches!(&e.event, Event::Budget(budget) if budget.kind == BudgetKind::Exceeded));
    assert!(exceeded, "{:?}", kinds(&events));
    assert_eq!(result(&events).subtype, ResultSubtype::Error);
    assert_totals(&events, "capped");
    assert_eq!(messages(&*stores, "s1").await, [Message::user_text("go")]);
}

/// Compare `got` with `want`; a tool result may be the interrupted
/// placeholder (a crash between a tool's start and its end).
fn compare(got: &[Message], want: &[Message], context: &str) {
    let placeholder = ToolResultBlock::text("", INTERRUPTED_TOOL_TEXT, true).content;
    assert_eq!(got.len(), want.len(), "{context}");
    for (got, want) in got.iter().zip(want) {
        assert_eq!(got.content.len(), want.content.len(), "{context}");
        for (got, want) in got.content.iter().zip(&want.content) {
            if let (ContentBlock::ToolResult(block), ContentBlock::ToolResult(expected)) =
                (got, want)
                && block.is_error
                && block.content == placeholder
            {
                assert_eq!(block.tool_use_id, expected.tool_use_id, "{context}");
                continue;
            }
            assert_eq!(got, want, "{context}");
        }
    }
}

/// A crash at every write of a run with an `after_provider_call` chain,
/// before and after the write lands, then a resume: the session is the
/// clean run's, the totals are the sum of every receipt, no tool runs
/// twice, and each turn has one answered receipt.
#[tokio::test]
async fn a_crash_at_any_write_of_a_hooked_run_keeps_every_receipt() {
    let clean = Arc::new(Stores::default());
    let agent = build(
        spec(),
        Arc::new(Replay::new(turns())),
        accepting(),
        clean.clone(),
        &Runs::default(),
    );
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let want = messages(&*clean, "s1").await;
    let writes = clean.log.lock().unwrap().len() as u64;
    let mut orphans = 0;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
            let provider = Arc::new(Replay::new(turns()));
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = build(spec(), provider.clone(), accepting(), faulty, &runs)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = build(spec(), provider.clone(), accepting(), stores.clone(), &runs);
            let run_id = match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => run_id,
                Ok(_) => {
                    finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
                    run_id
                }
                Err(_) => {
                    let stream = agent.run("go", RunOptions::new("s1"));
                    let run_id = stream.run_id().clone();
                    finish(stream).await;
                    run_id
                }
            };
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            compare(&messages(&*stores, "s1").await, &want, &context);
            let ran = runs.lock().unwrap().clone();
            assert!(ran.values().all(|&n| n == 1), "{context}: {ran:?}");
            let events = durable(&stores, &run_id).await;
            assert_totals(&events, &context);
            let counts = accepted(&events);
            let calls = provider.calls();
            for turn in 0..turns().len() {
                let (answered, orphaned) = counts.get(&turn).copied().unwrap_or_default();
                assert_eq!(answered, 1, "{context}: turn {turn}");
                assert!(orphaned <= 1, "{context}: turn {turn}");
                orphans += orphaned;
                let spare = (receipts(&events).iter())
                    .filter(|r| turn_of(&r.logical_call_id) == turn)
                    .filter(|r| r.disposition == CallDisposition::Cancelled)
                    .count() as u32;
                let made = calls.get(&turn).copied().unwrap_or(0);
                assert!(
                    made >= 1 && made <= 1 + orphaned + spare,
                    "{context}: turn {turn} called {made} times"
                );
            }
        }
    }
    assert!(
        orphans > 0,
        "some crash fell between a receipt and its response"
    );
}

/// Records every commit's event kinds and checkpoint bytes.
#[derive(Default)]
struct Recording {
    stores: Stores,
    commits: Mutex<Vec<(Vec<String>, Option<String>)>>,
}

#[async_trait]
impl RunStore for Recording {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        RunStore::create(&self.stores, run).await
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.stores.claim(ns, run_id, owner).await
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        RunStore::load(&self.stores, ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        let kinds = commit.events.iter().map(|e| kind(&e.event)).collect();
        let checkpoint = (commit.checkpoint.as_ref()).map(|c| serde_json::to_string(c).unwrap());
        self.commits.lock().unwrap().push((kinds, checkpoint));
        self.stores.commit(lease, commit).await
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.stores.events(ns, run_id, after).await
    }
}

#[async_trait]
impl SessionStore for Recording {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        SessionStore::create(&self.stores, ns, session).await
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&self.stores, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        self.stores.append(ns, session, expected, entries).await
    }
}

async fn recorded(hooks: Hooks) -> Vec<(Vec<String>, Option<String>)> {
    let stores = Arc::new(Recording::default());
    let provider = Arc::new(Replay::new(turns()));
    let agent = build(spec(), provider, hooks, stores.clone(), &Runs::default());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    stores.commits.lock().unwrap().clone()
}

/// Without a hook between the stream and the response, a run writes as it
/// always did: the receipt rides the `AssistantReceived` write-through,
/// byte for byte.
#[tokio::test]
async fn without_hooks_the_receipt_rides_the_response_write() {
    let commits = recorded(Hooks::default()).await;
    let shapes: Vec<(Vec<&str>, Option<&str>)> = (commits.iter())
        .map(|(kinds, checkpoint)| {
            let kinds = kinds.iter().map(String::as_str).collect();
            (kinds, checkpoint.as_deref())
        })
        .collect();
    let expected: Vec<(Vec<&str>, Option<&str>)> = (EXPECTED_WITHOUT_HOOKS.iter())
        .map(|(kinds, checkpoint)| (kinds.to_vec(), *checkpoint))
        .collect();
    assert_eq!(shapes, expected);

    // The same run with an accepting chain writes once more per call: the
    // receipt and its usage, under `ProviderPending`, before the hooks.
    let hooked = recorded(accepting()).await;
    assert_eq!(hooked.len(), commits.len() + 2);
    let early: Vec<_> = (hooked.iter())
        .filter(|(kinds, _)| kinds.first().map(String::as_str) == Some("model_call_receipt"))
        .collect();
    assert_eq!(early.len(), 2, "{hooked:?}");
    for (kinds, checkpoint) in early {
        assert_eq!(kinds, &["model_call_receipt", "usage"]);
        assert!(checkpoint.as_deref().unwrap().contains("provider_pending"));
    }
}

#[allow(clippy::type_complexity)]
const EXPECTED_WITHOUT_HOOKS: &[(&[&str], Option<&str>)] = &[
    (
        &["context_build", "model_call_delivery"],
        Some(
            r#"{"turn":0,"usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"session_len":2,"phase":{"phase":"provider_pending","attempt":1}}"#,
        ),
    ),
    (
        &["model_call_receipt", "assistant", "usage"],
        Some(
            r#"{"turn":0,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"cost_usd":0.02,"session_len":2,"phase":{"phase":"assistant_received","message":{"role":"assistant","content":[{"type":"text","text":"working"},{"type":"tool_use","id":"call_0_0","name":"Read","input":{"value":"v0.0"}}]},"stop_reason":"tool_use"}}"#,
        ),
    ),
    (
        &["tool_call_start"],
        Some(
            r#"{"turn":0,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"cost_usd":0.02,"session_len":3,"phase":{"phase":"tool_batch","decisions":[{"tool_use_id":"call_0_0","decision":{"type":"allow"}}],"after_seq":7}}"#,
        ),
    ),
    (&["tool_call_end"], None),
    (
        &["user"],
        Some(
            r#"{"turn":0,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"cost_usd":0.02,"session_len":3,"phase":{"phase":"tool_results","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_0_0","content":[{"type":"text","text":"v0.0"}]}]}}}"#,
        ),
    ),
    (
        &["context_build", "model_call_delivery"],
        Some(
            r#"{"turn":1,"usage":{"input_tokens":10,"output_tokens":5,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"cost_usd":0.02,"session_len":4,"phase":{"phase":"provider_pending","attempt":1}}"#,
        ),
    ),
    (
        &["model_call_receipt", "assistant", "usage"],
        Some(
            r#"{"turn":1,"usage":{"input_tokens":20,"output_tokens":10,"cache_read_tokens":0,"cache_creation_tokens":0},"usage_complete":true,"cost_usd":0.04,"session_len":4,"phase":{"phase":"assistant_received","message":{"role":"assistant","content":[{"type":"text","text":"done"}]},"stop_reason":"end_turn"}}"#,
        ),
    ),
    (&["result"], None),
];
