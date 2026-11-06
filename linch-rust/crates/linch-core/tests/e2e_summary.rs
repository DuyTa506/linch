//! Compaction summaries that were paid but gave no summary: an empty one
//! (thinking only) and a failed one that reported usage. Their usage is
//! reported like any other call's.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::ResultEvent;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, CompactionSpec, Envelope, Event, Hooks, PermissionMode, Provider, ProviderError,
    ProviderRequest, ProviderStream, Role, RunOptions, RunStore, ScriptedTurn, SessionStore,
    ToolChoice, ToolScope, Usage,
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
