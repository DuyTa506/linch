//! Compaction in a run: it triggers past the threshold, logs a projection,
//! keeps the cached prefix byte-identical, pays each summary once, and
//! recovers from a crash at every write.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::CompactionEvent;
use linch_core::events::model_call::{CallDisposition, ModelCallKind};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, CompactionSpec, ContentBlock, Envelope, Event, LogEntry, Message, PermissionMode,
    Provider, ProviderError, ProviderRequest, ProviderStream, Role, RunId, RunOptions, RunStatus,
    RunStore, SessionId, SessionLog, SessionStore, ToolChoice, ToolScope, Usage,
};
use tokio_util::sync::CancellationToken;

/// Answers by the request: a summary request (tool use disabled) gets
/// `summary of {n} messages`; a primary request gets the turn after the
/// last tool result it carries. Counts calls per request key.
struct Keyed {
    turns: Vec<linch_core::ScriptedTurn>,
    calls: Mutex<HashMap<String, u32>>,
    requests: Mutex<Vec<ProviderRequest>>,
    /// Fail every summary request with a 503.
    failing_summaries: bool,
}

impl Keyed {
    fn new() -> Arc<Self> {
        Arc::new(Self::answering())
    }

    fn answering() -> Self {
        Self {
            turns: script(3, 1, "Read"),
            calls: Mutex::default(),
            requests: Mutex::default(),
            failing_summaries: false,
        }
    }
}

/// The primary turn a request asks for: 0 for the prompt, `t + 1` after
/// the results of `call_{t}_*`.
fn primary_turn(request: &ProviderRequest) -> usize {
    let last = request.messages.last().unwrap();
    match &last.content[0] {
        ContentBlock::ToolResult(result) => {
            let turn = result.tool_use_id.split('_').nth(1).unwrap();
            turn.parse::<usize>().unwrap() + 1
        }
        _ => 0,
    }
}

#[async_trait]
impl Provider for Keyed {
    fn id(&self) -> &str {
        "keyed"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.requests.lock().unwrap().push(request.clone());
        let summarizing = request.tool_choice == Some(ToolChoice::Disabled {});
        if summarizing && self.failing_summaries {
            return Err(ProviderError::Status {
                status: 503,
                retry_after: None,
                message: "busy".into(),
            });
        }
        let (key, turn) = if summarizing {
            let n = request.messages.len();
            let summary = TurnBuilder::new(MODEL)
                .text(format!("summary of {n} messages"))
                .usage(usage(3, 2))
                .build();
            (format!("summary:{n}"), summary)
        } else {
            let turn = primary_turn(&request);
            (format!("primary:{turn}"), self.turns[turn].clone())
        };
        *self.calls.lock().unwrap().entry(key).or_default() += 1;
        let events: Vec<_> = turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

fn compacting_agent<S: RunStore + SessionStore + 'static>(
    provider: Arc<Keyed>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, runs)]);
    agent_with(
        spec,
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        stores,
    )
}

async fn entries(stores: &Stores) -> Vec<LogEntry> {
    SessionStore::load(stores, NS, &SessionId::new("s1"))
        .await
        .unwrap()
}

fn view(entries: Vec<LogEntry>) -> Vec<Message> {
    let mut log = SessionLog::new();
    for entry in entries {
        match entry {
            LogEntry::Message(m) => log.append((*m.message).clone(), m.visible, m.historical),
            LogEntry::Projection(p) => {
                log.record_projection(p.replacement, p.reason);
            }
        }
    }
    log.provider_view().iter().map(|m| (**m).clone()).collect()
}

#[tokio::test]
async fn compaction_logs_a_projection_and_keeps_the_prefix() {
    let stores = Arc::new(Stores::default());
    let provider = Keyed::new();
    let agent = compacting_agent(provider.clone(), &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;

    let log = entries(&stores).await;
    let projections = (log.iter())
        .filter(|e| matches!(e, LogEntry::Projection(_)))
        .count();
    assert_eq!(projections, 2, "turns 2 and 3 compact");
    let compacted = view(log);
    let summary = Message::user_text("<summary of earlier conversation>\n\nsummary of 4 messages");
    assert_eq!(compacted[..2], [Message::user_text("go"), summary]);
    assert_eq!(
        compacted.len(),
        5,
        "the first message, the summary, the last turn"
    );

    let requests = provider.requests.lock().unwrap().clone();
    let (summaries, primaries): (Vec<_>, Vec<_>) =
        (requests.iter()).partition(|r| r.tool_choice == Some(ToolChoice::Disabled {}));
    assert_eq!((summaries.len(), primaries.len()), (2, 4));
    let bytes = |m: &Message| serde_json::to_string(m).unwrap();
    for request in &primaries {
        assert_eq!(request.system, primaries[0].system, "the system prompt");
        assert_eq!(
            bytes(&request.messages[0]),
            bytes(&Message::user_text("go"))
        );
    }
    assert_eq!(primaries[2].messages[1].role, Role::User);
    assert!(primaries[2].messages[1].text().starts_with("<summary"));
    assert_eq!(summaries[0].messages[0], Message::user_text("go"));

    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let paid: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(r) if r.kind == ModelCallKind::Compaction => Some(r),
            _ => None,
        })
        .collect();
    assert_eq!(paid.len(), 2);
    assert!(
        paid.iter()
            .all(|r| r.disposition == CallDisposition::Accepted)
    );
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    let primary = usage(10, 5);
    let total = (0..4).map(|_| primary).chain([usage(3, 2); 2]);
    assert_eq!(
        result.total_usage,
        total.fold(Usage::default(), |a, u| a + u)
    );
    let reports: Vec<&CompactionEvent> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Compaction(c) => Some(c),
            _ => None,
        })
        .collect();
    assert_eq!(
        (reports[0].messages_before, reports[0].messages_after),
        (5, 4)
    );
    assert_eq!(
        (reports.len(), reports[1].strategy.as_str()),
        (2, "summary")
    );
    // 3 tool turns and the answer (18), plus two writes per compaction.
    assert_eq!(stores.runs.writes().commit, 18 + 2 * 2);
    assert_eq!(stores.sessions.writes().append, 8 + 2);
}

#[tokio::test]
async fn compaction_is_off_by_default_and_below_its_threshold() {
    for compaction in [None, Some(CompactionSpec::new(1_000_000))] {
        let stores = Arc::new(Stores::default());
        let provider = Keyed::new();
        let runs = Runs::default();
        let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
        let permissions = policy(PermissionMode::SkipDangerous);
        let mut spec = spec();
        spec.compaction = compaction;
        let agent = agent_with(spec, provider.clone(), tools, permissions, stores.clone());
        finish(agent.run("go", RunOptions::new("s1"))).await;
        let log = entries(&stores).await;
        assert!(log.iter().all(|e| matches!(e, LogEntry::Message(_))));
        assert_eq!(
            provider.requests.lock().unwrap().len(),
            4,
            "no summary call"
        );
        assert_eq!(stores.runs.writes().commit, 18);
    }
}

#[tokio::test]
async fn a_failed_summary_leaves_the_turn_uncompacted() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(Keyed {
        failing_summaries: true,
        ..Keyed::answering()
    });
    let agent = compacting_agent(provider, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let log = entries(&stores).await;
    assert!(log.iter().all(|e| matches!(e, LogEntry::Message(_))));
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let failed = (events.iter())
        .filter(|e| {
            matches!(&e.event, Event::ModelCallReceipt(r)
            if r.kind == ModelCallKind::Compaction && r.disposition == CallDisposition::Failed)
        })
        .count();
    assert_eq!(failed, 2, "each turn past the threshold tried once");
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert_eq!(
        result.subtype,
        linch_core::events::kinds::ResultSubtype::Success
    );
    assert!(
        !result.usage_complete,
        "the failed summaries reported no usage"
    );
    // A failed summary's receipt rides the primary call's barrier.
    assert_eq!(stores.runs.writes().commit, 18 + 2);
}

#[test]
fn a_compaction_spec_that_cannot_run_is_refused() {
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec::new(0));
    let built = linch_core::AgentBuilder::new(spec)
        .provider(Keyed::new())
        .permissions(policy(PermissionMode::Default))
        .run_store(Arc::new(Stores::default()))
        .session_store(Arc::new(Stores::default()))
        .namespace(NS)
        .build();
    assert!(matches!(built, Err(linch_core::BuildError::Invalid(_))));
}

/// The log with every tool result's content dropped: a crash on a tool
/// barrier may leave the interrupted placeholder in place of a result.
fn shape(entries: &[LogEntry]) -> String {
    let mut value = serde_json::to_value(view(entries.to_vec())).unwrap();
    strip(&mut value);
    let projections = (entries.iter())
        .filter(|e| matches!(e, LogEntry::Projection(_)))
        .count();
    format!("{projections} projections, view {value}")
}

fn strip(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::Object(map) => {
            if map.get("type").and_then(|t| t.as_str()) == Some("tool_result") {
                map.remove("content");
                map.remove("is_error");
            }
            map.values_mut().for_each(strip);
        }
        serde_json::Value::Array(items) => items.iter_mut().for_each(strip),
        _ => {}
    }
}

/// Every logical call has one accepted receipt, and every extra provider
/// call was settled by an abandoned one.
fn check_paid_once(provider: &Keyed, events: &[Envelope], context: &str) {
    let mut accepted: HashMap<String, u32> = HashMap::new();
    let mut abandoned: HashMap<String, u32> = HashMap::new();
    for envelope in events {
        if let Event::ModelCallReceipt(r) = &envelope.event {
            let map = match r.disposition {
                CallDisposition::Accepted => &mut accepted,
                CallDisposition::Cancelled => &mut abandoned,
                other => panic!("{context}: unexpected receipt {other:?}"),
            };
            *map.entry(r.logical_call_id.clone()).or_default() += 1;
        }
    }
    assert!(
        accepted.values().all(|&n| n == 1),
        "{context}: {accepted:?}"
    );
    assert_eq!(accepted.len(), 6, "{context}: four turns, two summaries");
    let calls: u32 = provider.calls.lock().unwrap().values().sum();
    let spare: u32 = abandoned.values().sum();
    assert!(
        calls <= 6 + spare,
        "{context}: {calls} calls, {spare} abandoned"
    );
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_compacting_run_recovers() {
    let clean = Arc::new(Stores::default());
    finish(
        compacting_agent(Keyed::new(), &Runs::default(), clean.clone())
            .run("go", RunOptions::new("s1")),
    )
    .await;
    let want = shape(&entries(&clean).await);
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let provider = Keyed::new();
            let runs = Runs::default();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream =
                compacting_agent(provider.clone(), &runs, faulty).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = compacting_agent(provider.clone(), &runs, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            assert_eq!(shape(&entries(&stores).await), want, "{context}");
            assert!(runs.lock().unwrap().values().all(|&n| n == 1), "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            check_paid_once(&provider, &events, &context);
        }
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
