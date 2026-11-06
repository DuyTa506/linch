//! Compaction summaries that were paid but gave no summary: an empty one
//! (thinking only) and a failed one that reported usage. Their receipt and
//! usage are durable at once, so neither a crash nor a dropped stream buys
//! them again, and their usage is reported like any other call's.

mod common;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::{FutureExt, StreamExt};
use linch_core::events::kinds::ResultEvent;
use linch_core::events::model_call::{CallDisposition, ModelCallKind};
use linch_core::hooks::{ContextBuild, ContextBuildRequest, ContextBuildResponse};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, CallbackError, CompactionSpec, Envelope, Event, Hook, Hooks, PermissionMode, Provider,
    ProviderError, ProviderRequest, ProviderStream, Role, RunId, RunOptions, RunStatus, RunStore,
    ScriptedTurn, SessionStore, ToolChoice, ToolScope, Usage,
};
use tokio_util::sync::CancellationToken;

/// How the provider answers a summary request.
#[derive(Clone, Copy, Debug)]
enum Summary {
    /// Thinking only, with usage: accepted, but no text.
    Empty,
    /// Usage, then a transport error: failed, but paid.
    Failed,
}

/// What every summary costs.
fn summary_usage() -> Usage {
    usage(30, 40)
}

/// Answers a summary request per [`Summary`], and a primary request with
/// the turn its assistant messages count gives (no summary is ever
/// applied, so the count is the turn). Counts calls per kind.
struct Summaries {
    summary: Summary,
    turns: Vec<ScriptedTurn>,
    calls: Mutex<HashMap<&'static str, u32>>,
}

impl Summaries {
    fn new(summary: Summary) -> Arc<Self> {
        Arc::new(Self {
            summary,
            turns: script(3, 1, "Read"),
            calls: Mutex::default(),
        })
    }

    fn calls(&self, kind: &str) -> u32 {
        self.calls.lock().unwrap().get(kind).copied().unwrap_or(0)
    }
}

#[async_trait]
impl Provider for Summaries {
    fn id(&self) -> &str {
        "summaries"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let summarizing = request.tool_choice == Some(ToolChoice::Disabled {});
        let kind = if summarizing { "summary" } else { "primary" };
        *self.calls.lock().unwrap().entry(kind).or_default() += 1;
        if !summarizing {
            let turn = (request.messages.iter())
                .filter(|message| message.role == Role::Assistant)
                .count();
            let events = self.turns[turn].events.iter().cloned().map(Ok);
            return Ok(futures::stream::iter(events.collect::<Vec<_>>()).boxed());
        }
        let turn = TurnBuilder::new(MODEL)
            .thinking("nothing to add", None)
            .usage(summary_usage())
            .build();
        let mut items: Vec<_> = turn.events.into_iter().map(Ok).collect();
        if let Summary::Failed = self.summary {
            items.push(Err(ProviderError::Transport {
                message: "reset".into(),
            }));
        }
        Ok(futures::stream::iter(items).boxed())
    }
}

/// An agent that compacts every turn it can, keeping one turn verbatim.
fn compacting<S: RunStore + SessionStore + 'static>(
    provider: Arc<Summaries>,
    hooks: Arc<Hooks>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    linch_core::AgentBuilder::new(spec)
        .provider(provider)
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(hooks)
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

fn result(envelopes: &[Envelope]) -> &ResultEvent {
    match envelopes.last().map(|e| &e.event) {
        Some(Event::Result(result)) => result,
        other => panic!("no result: {other:?}"),
    }
}

/// Each `Usage` event adds its usage to the one before, and the last one
/// is the result's total.
fn check_usage_events(envelopes: &[Envelope]) {
    let mut total = Usage::default();
    for envelope in envelopes {
        if let Event::Usage(paid) = &envelope.event {
            total += paid.usage;
            assert_eq!(paid.cumulative, total, "seq {}", envelope.seq);
        }
    }
    assert_eq!(result(envelopes).total_usage, total);
}

/// R1-core 4: a summary that gave no text still reports its usage.
#[tokio::test]
async fn a_paid_summary_without_text_reports_its_usage() {
    for summary in [Summary::Empty, Summary::Failed] {
        let stores = Arc::new(Stores::default());
        let provider = Summaries::new(summary);
        let hooks = Arc::new(Hooks::default());
        let agent = compacting(provider.clone(), hooks, &Runs::default(), stores.clone());
        let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;
        assert_eq!(provider.calls("summary"), 2, "{summary:?}: turns 2 and 3");
        check_usage_events(&envelopes);
        let primary = (0..4).map(|_| usage(10, 5));
        let total = primary
            .chain([summary_usage(); 2])
            .fold(Usage::default(), |a, u| a + u);
        let result = result(&envelopes);
        assert_eq!(result.total_usage, total, "{summary:?}");
        assert!(result.usage_complete, "{summary:?}");
    }
}

/// A `context_build` hook that hangs on its `hang_at`-th call and adds
/// nothing otherwise.
struct Hangs {
    hang_at: u32,
    calls: AtomicU32,
}

#[async_trait]
impl Hook<ContextBuild> for Hangs {
    fn policy_id(&self) -> &str {
        "hangs"
    }
    fn policy_version(&self) -> u32 {
        1
    }
    async fn call(
        &self,
        _request: &ContextBuildRequest,
    ) -> Result<ContextBuildResponse, CallbackError> {
        if self.calls.fetch_add(1, Ordering::SeqCst) + 1 == self.hang_at {
            std::future::pending::<()>().await;
        }
        Ok(ContextBuildResponse::default())
    }
}

/// Every logical call has exactly one receipt that is not an abandoned
/// one; the provider was called no more often than there are receipts of
/// each kind (an abandoned receipt settles a call whose own receipt was
/// never written); and the result counts every receipt.
fn check_paid_once(provider: &Summaries, events: &[Envelope], context: &str) {
    let mut settled: HashMap<&str, u32> = HashMap::new();
    let mut receipts: HashMap<&str, u32> = HashMap::new();
    let (mut paid, mut complete) = (Usage::default(), true);
    for envelope in events {
        let Event::ModelCallReceipt(receipt) = &envelope.event else {
            continue;
        };
        let kind = match receipt.kind {
            ModelCallKind::Primary => "primary",
            ModelCallKind::Compaction => "summary",
        };
        *receipts.entry(kind).or_default() += 1;
        if receipt.disposition != CallDisposition::Cancelled {
            *settled.entry(&receipt.logical_call_id).or_default() += 1;
        }
        match receipt.usage {
            Some(usage) => paid += usage,
            None => complete = false,
        }
    }
    assert!(settled.values().all(|&n| n == 1), "{context}: {settled:?}");
    assert_eq!(settled.len(), 6, "{context}: four turns, two summaries");
    for kind in ["primary", "summary"] {
        let calls = provider.calls(kind);
        let settled = receipts.get(kind).copied().unwrap_or(0);
        assert!(calls <= settled, "{context}: {kind} called {calls} times");
    }
    let result = result(events);
    assert_eq!(result.total_usage, paid, "{context}: every receipt counts");
    assert_eq!(result.usage_complete, complete, "{context}");
}

/// R1-core 1: the stream is dropped while `context_build` waits, right
/// after a summary that gave no text was paid. Resume goes on to the turn's
/// primary call and does not buy that summary again.
#[tokio::test]
async fn a_stream_dropped_after_a_paid_summary_does_not_pay_it_again() {
    for summary in [Summary::Empty, Summary::Failed] {
        let stores = Arc::new(Stores::default());
        let provider = Summaries::new(summary);
        let hooks = Hooks::default();
        // Turn 2's context, right after its summary.
        let hangs = Hangs {
            hang_at: 3,
            calls: AtomicU32::new(0),
        };
        let _ = hooks.context_build.register(Arc::new(hangs)).unwrap();
        let hooks = Arc::new(hooks);
        let runs = Runs::default();
        let agent = compacting(provider.clone(), hooks, &runs, stores.clone());
        let mut stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        // Memory stores never wait: the run is pending only in the hook.
        while let Some(item) = stream.next().now_or_never() {
            item.expect("the run waits in the hook").unwrap();
        }
        assert_eq!(provider.calls("summary"), 1, "{summary:?}");
        drop(stream);
        finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
        let record = stores.runs.load(NS, &run_id).await.unwrap();
        assert_eq!(record.status, RunStatus::Completed, "{summary:?}");
        assert_eq!(provider.calls("summary"), 2, "{summary:?}: turns 2 and 3");
        let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
        check_paid_once(&provider, &events, &format!("{summary:?}"));
    }
}

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
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
    }
}

#[tokio::test]
async fn a_crash_at_any_write_around_a_paid_summary_recovers() {
    for summary in [Summary::Empty, Summary::Failed] {
        let clean = Arc::new(Stores::default());
        let hooks = Arc::new(Hooks::default());
        let agent = compacting(
            Summaries::new(summary),
            hooks.clone(),
            &Runs::default(),
            clean.clone(),
        );
        finish(agent.run("go", RunOptions::new("s1"))).await;
        let writes = clean.log.lock().unwrap().len() as u64;
        for n in 1..=writes {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                let context = format!("{summary:?}: write {n} failing {timing:?}");
                let stores = Arc::new(Stores::default());
                let provider = Summaries::new(summary);
                let runs = Runs::default();
                let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
                let stream = compacting(provider.clone(), hooks.clone(), &runs, faulty)
                    .run("go", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                assert!(drain(stream).await.1.is_some(), "{context}");
                let agent = compacting(provider.clone(), hooks.clone(), &runs, stores.clone());
                let run_id = recover(&agent, &stores, run_id).await;
                let record = stores.runs.load(NS, &run_id).await.unwrap();
                assert_eq!(record.status, RunStatus::Completed, "{context}");
                assert!(runs.lock().unwrap().values().all(|&n| n == 1), "{context}");
                let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
                check_paid_once(&provider, &events, &context);
            }
        }
    }
}
