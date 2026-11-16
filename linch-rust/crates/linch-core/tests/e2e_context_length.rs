//! Context-length recovery: a provider that refuses a prompt as longer
//! than its context window gets a shorter one. The run clears old tool
//! results (micro-compaction) or summarizes, forced, then calls again; the
//! next call is the turn's next delivery attempt, never a provider retry.
//! It is bounded, and a crash at any write recovers without looping.

mod common;

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::compaction::CLEARED_RESULT;
use linch_core::events::kinds::CompactionEvent;
use linch_core::events::model_call::{CallDisposition, ModelCallKind};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, CompactionSpec, ContentBlock, Envelope, Event, LogEntry, PermissionMode, Provider,
    ProviderError, ProviderRequest, ProviderStream, RunId, RunOptions, RunStatus, RunStore,
    ScriptedTurn, SessionId, SessionStore, ToolChoice, ToolResultPart, ToolScope, Usage,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// Bytes of each tool result: well over the cleared placeholder.
const BIG: usize = 400;

/// When the provider refuses a primary request as too long.
type Refuse = fn(&ProviderRequest) -> bool;

/// Answers summary requests with `summary of {n} messages` (or fails them
/// with a 503 when `failing_summaries`) and primary requests by their turn,
/// unless `refuse` says the prompt does not fit: then a 400 context-length
/// refusal before any event. Counts calls per key.
struct Window {
    turns: Vec<ScriptedTurn>,
    refuse: Refuse,
    failing_summaries: bool,
    calls: Mutex<HashMap<String, u32>>,
    summary_requests: Mutex<Vec<ProviderRequest>>,
}

impl Window {
    fn new(refuse: Refuse) -> Arc<Self> {
        Arc::new(Self {
            turns: big_script(3),
            refuse,
            failing_summaries: false,
            calls: Mutex::default(),
            summary_requests: Mutex::default(),
        })
    }

    fn failing(refuse: Refuse) -> Arc<Self> {
        Arc::new(Self {
            failing_summaries: true,
            ..Arc::into_inner(Self::new(refuse)).unwrap()
        })
    }

    fn calls(&self, prefix: &str) -> u32 {
        let calls = self.calls.lock().unwrap();
        (calls.iter())
            .filter(|(key, _)| key.starts_with(prefix))
            .map(|(_, n)| n)
            .sum()
    }

    fn count(&self, key: String) {
        *self.calls.lock().unwrap().entry(key).or_default() += 1;
    }
}

/// `n` turns that each call `Read` once with a big value, then an answer.
fn big_script(n: usize) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = (0..n)
        .map(|t| {
            let value = json!({"value": format!("v{t}{}", "x".repeat(BIG))});
            TurnBuilder::new(MODEL)
                .text("working")
                .tool_call(&format!("call_{t}_0"), "Read", &value)
                .usage(usage(10, 5))
                .build()
        })
        .collect();
    turns.push(text_turn("done"));
    turns
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

/// Tool results in `request` that micro-compaction did not clear. Each
/// counts as big, the interrupted placeholder of a crash included, so a
/// crash does not change which prompts fit.
fn big_results(request: &ProviderRequest) -> usize {
    (request.messages.iter())
        .flat_map(|message| &message.content)
        .filter(|block| match block {
            ContentBlock::ToolResult(result) => !result.content.iter().any(
                |part| matches!(part, ToolResultPart::Text(text) if text.text == CLEARED_RESULT),
            ),
            _ => false,
        })
        .count()
}

/// Refuses a prompt that holds two big results or more.
fn two_big(request: &ProviderRequest) -> bool {
    big_results(request) >= 2
}

/// Refuses any prompt past the first tool turn that holds a big result:
/// nothing shortens the last turn's result.
fn any_big_late(request: &ProviderRequest) -> bool {
    primary_turn(request) >= 2 && big_results(request) >= 1
}

#[async_trait]
impl Provider for Window {
    fn id(&self) -> &str {
        "window"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let turn = if request.tool_choice == Some(ToolChoice::Disabled {}) {
            self.count("summary".into());
            self.summary_requests.lock().unwrap().push(request.clone());
            if self.failing_summaries {
                return Err(ProviderError::Status {
                    status: 503,
                    retry_after: None,
                    message: "busy".into(),
                });
            }
            let n = request.messages.len();
            TurnBuilder::new(MODEL)
                .text(format!("summary of {n} messages"))
                .usage(usage(3, 2))
                .build()
        } else {
            let turn = primary_turn(&request);
            if (self.refuse)(&request) {
                self.count(format!("refused:{turn}"));
                return Err(ProviderError::ContextLength {
                    status: Some(400),
                    message: "prompt is too long: 9 tokens > 8 maximum".into(),
                });
            }
            self.count(format!("primary:{turn}"));
            self.turns[turn].clone()
        };
        let events: Vec<_> = turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

/// An agent whose compaction never triggers at a turn's start (the window
/// the provider reports is the default 128k), keeping one turn.
fn agent_on<S: RunStore + SessionStore + 'static>(
    provider: Arc<Window>,
    compaction: Option<CompactionSpec>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let mut spec = spec();
    spec.compaction = compaction;
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, runs)]);
    agent_with(
        spec,
        provider,
        tools,
        policy(PermissionMode::SkipDangerous),
        stores,
    )
}

fn summarizing() -> Option<CompactionSpec> {
    Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::default()
    })
}

fn micro() -> Option<CompactionSpec> {
    Some(CompactionSpec {
        micro: true,
        ..summarizing().unwrap()
    })
}

fn compactions(events: &[Envelope]) -> Vec<CompactionEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::Compaction(c) => Some(c.clone()),
            _ => None,
        })
        .collect()
}

fn count(events: &[Envelope], kind_name: &str) -> usize {
    (events.iter())
        .filter(|e| kind(&e.event) == kind_name)
        .count()
}

/// Every delivery has its own attempt id: a forced compaction never
/// reuses one, before or after a crash.
fn check_unique_deliveries(events: &[Envelope], context: &str) {
    let mut ids = HashSet::new();
    for envelope in events {
        if let Event::ModelCallDelivery(d) = &envelope.event {
            assert!(
                ids.insert(d.delivery_attempt_id.clone()),
                "{context}: {} delivered twice",
                d.delivery_attempt_id
            );
        }
    }
}

fn error_of(events: &[Envelope]) -> Option<serde_json::Value> {
    events.iter().rev().find_map(|e| match &e.event {
        Event::Error(error) => Some(serde_json::Value::Object(error.error.clone())),
        _ => None,
    })
}

async fn run(agent: &Agent, stores: &Stores) -> (RunId, Vec<Envelope>) {
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let _ = drain(stream).await;
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    (run_id, events)
}

async fn status(stores: &Stores, run_id: &RunId) -> RunStatus {
    stores.runs.load(NS, run_id).await.unwrap().status
}

async fn entries(stores: &Stores) -> Vec<LogEntry> {
    SessionStore::load(stores, NS, &SessionId::new("s1"))
        .await
        .unwrap()
}

#[tokio::test]
async fn a_refused_prompt_is_summarized_and_called_again() {
    let stores = Arc::new(Stores::default());
    let provider = Window::new(two_big);
    let agent = agent_on(
        provider.clone(),
        summarizing(),
        &Runs::default(),
        stores.clone(),
    );
    let (run_id, events) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
    // Turns 2 and 3 each hold two big results until a summary folds one.
    assert_eq!(provider.calls("refused"), 2);
    assert_eq!(provider.calls("summary"), 2);
    let reports = compactions(&events);
    assert_eq!(reports.len(), 2);
    assert!(reports.iter().all(|c| c.forced && c.strategy == "summary"));
    assert_eq!(count(&events, "provider_retry"), 0, "not a provider retry");
    check_unique_deliveries(&events, "clean");
    // The refused call is settled, free: a 400 before any event.
    let refused: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(r) if r.disposition == CallDisposition::Failed => Some(r),
            _ => None,
        })
        .collect();
    assert_eq!(refused.len(), 2);
    assert!(refused.iter().all(|r| r.usage == Some(Usage::default())));
    assert!(refused[0].delivery_attempt_id.ends_with(":primary:2:1"));
    let next: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallDelivery(d) if d.kind == ModelCallKind::Primary => {
                Some(d.delivery_attempt_id.clone())
            }
            _ => None,
        })
        .collect();
    assert!(next.contains(&format!("{run_id}:primary:2:2")), "{next:?}");
    let projections = (entries(&stores).await.iter())
        .filter(|e| matches!(e, LogEntry::Projection(p) if p.reason == "compaction"))
        .count();
    assert_eq!(projections, 2);
}

#[tokio::test]
async fn micro_compaction_answers_a_refusal_without_a_summary() {
    let stores = Arc::new(Stores::default());
    let provider = Window::new(two_big);
    let agent = agent_on(provider.clone(), micro(), &Runs::default(), stores.clone());
    let (run_id, events) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
    assert_eq!(provider.calls("refused"), 2);
    assert_eq!(provider.calls("summary"), 0, "clearing was enough");
    let reports = compactions(&events);
    assert_eq!(reports.len(), 2);
    for report in &reports {
        assert!(report.forced);
        assert_eq!(report.strategy, "micro");
        assert_eq!(report.messages_before, report.messages_after);
        assert!(report.tokens_after < report.tokens_before);
    }
    assert_eq!(count(&events, "provider_retry"), 0);
    check_unique_deliveries(&events, "clean");
    let log = entries(&stores).await;
    let reasons: Vec<_> = (log.iter())
        .filter_map(|e| match e {
            LogEntry::Projection(p) => Some(p.reason.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(reasons, ["micro-compaction", "micro-compaction"]);
    // The full history keeps every result whole; only the view is cleared.
    let full = (log.iter())
        .filter_map(|e| match e {
            LogEntry::Message(m) => Some(&m.message),
            _ => None,
        })
        .filter(|m| {
            m.content
                .iter()
                .any(|b| matches!(b, ContentBlock::ToolResult(_)))
        })
        .count();
    assert_eq!(full, 3);
}

/// A prompt nothing can shorten ends the run with the refusal, after at
/// most the summaries it allows; it never retries the refused prompt.
#[tokio::test]
async fn a_refusal_nothing_shortens_ends_the_run_with_the_refusal() {
    for (compaction, summaries) in [
        (None, 0),
        (summarizing(), 1),
        (micro(), 1),
        (
            Some(CompactionSpec {
                max_forced: 0,
                ..summarizing().unwrap()
            }),
            0,
        ),
    ] {
        let context = format!("{compaction:?}");
        let stores = Arc::new(Stores::default());
        let provider = Window::new(any_big_late);
        let agent = agent_on(
            provider.clone(),
            compaction,
            &Runs::default(),
            stores.clone(),
        );
        let (run_id, events) = run(&agent, &stores).await;
        assert_eq!(
            status(&stores, &run_id).await,
            RunStatus::Failed,
            "{context}"
        );
        assert_eq!(provider.calls("summary"), summaries, "{context}");
        assert_eq!(count(&events, "provider_retry"), 0, "{context}");
        let error = error_of(&events).unwrap();
        assert_eq!(error["kind"], "context_length", "{context}");
        assert_eq!(error["retryable"], false, "{context}");
        assert_eq!(
            error["status"], 400,
            "{context}: the provider's own refusal"
        );
        check_unique_deliveries(&events, &context);
    }
}

/// `max_forced` bounds the forced summaries of a whole run.
#[tokio::test]
async fn forced_summaries_stop_at_the_run_bound() {
    let stores = Arc::new(Stores::default());
    let provider = Window::new(two_big);
    let compaction = Some(CompactionSpec {
        max_forced: 1,
        ..summarizing().unwrap()
    });
    let agent = agent_on(
        provider.clone(),
        compaction,
        &Runs::default(),
        stores.clone(),
    );
    let (run_id, events) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Failed);
    assert_eq!(provider.calls("summary"), 1);
    assert_eq!(
        provider.calls("refused"),
        2,
        "turn 3 is refused past the bound"
    );
    assert_eq!(error_of(&events).unwrap()["kind"], "context_length");
}

/// A forced summary that gives none leaves the prompt the provider
/// refused: the run ends instead of sending it again.
#[tokio::test]
async fn a_forced_summary_that_fails_ends_the_run() {
    let stores = Arc::new(Stores::default());
    let provider = Window::failing(two_big);
    let agent = agent_on(
        provider.clone(),
        summarizing(),
        &Runs::default(),
        stores.clone(),
    );
    let (run_id, events) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Failed);
    assert_eq!(
        (provider.calls("refused"), provider.calls("summary")),
        (1, 1)
    );
    let error = error_of(&events).unwrap();
    assert_eq!(error["kind"], "context_length");
    assert!(
        error["message"]
            .as_str()
            .unwrap()
            .contains("could not shorten")
    );
    assert_eq!(count(&events, "provider_retry"), 0);
}

/// A forced summary is model output too: the spec's response redaction
/// scrubs it before it is logged.
#[tokio::test]
async fn a_forced_summary_is_redacted() {
    use linch_core::redaction::{RedactionRule, RedactionSpec};
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let mut spec = spec();
    spec.compaction = summarizing();
    spec.redaction = Some(RedactionSpec {
        rules: vec![RedactionRule {
            pattern: r"summary of \d+ messages".into(),
            replacement: "[SUMMARY]".into(),
        }],
        response: true,
        tool_results: false,
        prompt: false,
    });
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(
        spec,
        Window::new(two_big),
        tools,
        permissions,
        stores.clone(),
    );
    let (run_id, _) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
    let summaries: Vec<String> = (entries(&stores).await.iter())
        .filter_map(|e| match e {
            LogEntry::Projection(p) => Some(p.replacement[1].text()),
            _ => None,
        })
        .collect();
    assert_eq!(summaries.len(), 2);
    for summary in summaries {
        assert!(summary.contains("[SUMMARY]"), "{summary}");
        assert!(!summary.contains("messages"), "{summary}");
    }
}

/// The provider view with each tool result's content dropped, and the
/// projections by reason: a crash on a tool barrier may leave the
/// interrupted placeholder in place of a result.
fn shape(entries: &[LogEntry]) -> String {
    let mut log = linch_core::SessionLog::new();
    let mut reasons = Vec::new();
    for entry in entries.iter().cloned() {
        match entry {
            LogEntry::Message(m) => log.append((*m.message).clone(), m.visible, m.historical),
            LogEntry::Projection(p) => {
                reasons.push(p.reason.clone());
                log.record_projection(p.replacement, p.reason);
            }
            LogEntry::Prefix(p) => log.record_prefix(p),
        }
    }
    let view: Vec<_> = log.provider_view().iter().map(|m| (**m).clone()).collect();
    let mut value = serde_json::to_value(view).unwrap();
    strip(&mut value);
    format!("{reasons:?}, view {value}")
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

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            let _ = drain(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
        }
        Err(_) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let _ = drain(stream).await;
            run_id
        }
    }
}

/// A crash at every write of a run that recovers from refusals ends where
/// the clean run does: same status, same view, each tool run once, no
/// delivery id used twice, no provider retry, every summary paid once.
async fn crash_everywhere(compaction: Option<CompactionSpec>, provider: fn() -> Arc<Window>) {
    let clean = Arc::new(Stores::default());
    let (clean_id, _) = run(
        &agent_on(
            provider(),
            compaction.clone(),
            &Runs::default(),
            clean.clone(),
        ),
        &clean,
    )
    .await;
    let want_status = status(&clean, &clean_id).await;
    let want = shape(&entries(&clean).await);
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("{compaction:?}: write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let provider = provider();
            let runs = Runs::default();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = agent_on(provider.clone(), compaction.clone(), &runs, faulty)
                .run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = agent_on(provider.clone(), compaction.clone(), &runs, stores.clone());
            let run_id = recover(&agent, &stores, run_id).await;
            assert_eq!(status(&stores, &run_id).await, want_status, "{context}");
            // A threshold reads the size of the view, and a crash on a tool
            // barrier may leave the short interrupted placeholder in place
            // of a result: the run then compacts later, which is right.
            let sized = compaction
                .as_ref()
                .is_some_and(|c| c.threshold_tokens.is_some());
            let barrier = clean.log.lock().unwrap()[n as usize - 1].is_tool_barrier();
            if !(sized && barrier) {
                assert_eq!(shape(&entries(&stores).await), want, "{context}");
            }
            assert!(runs.lock().unwrap().values().all(|&n| n == 1), "{context}");
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            check_unique_deliveries(&events, &context);
            assert_eq!(count(&events, "provider_retry"), 0, "{context}");
            let mut accepted: HashMap<String, u32> = HashMap::new();
            for envelope in &events {
                if let Event::ModelCallReceipt(r) = &envelope.event
                    && r.kind == ModelCallKind::Compaction
                    && r.disposition == CallDisposition::Accepted
                {
                    *accepted.entry(r.logical_call_id.clone()).or_default() += 1;
                }
            }
            assert!(
                accepted.values().all(|&n| n == 1),
                "{context}: {accepted:?}"
            );
        }
    }
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_forced_summary_recovers() {
    crash_everywhere(summarizing(), || Window::new(two_big)).await;
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_micro_compaction_recovers() {
    crash_everywhere(micro(), || Window::new(two_big)).await;
}

#[tokio::test]
async fn a_crash_at_any_write_of_an_unshortened_refusal_still_ends_the_run() {
    crash_everywhere(summarizing(), || Window::failing(two_big)).await;
    crash_everywhere(summarizing(), || Window::new(any_big_late)).await;
}

/// Never refuses: the view only compacts at a turn's start.
fn never(_: &ProviderRequest) -> bool {
    false
}

/// Micro-compaction at a turn's start, under a threshold one cleared
/// result gets back under at turn 2 but not at turn 3.
fn proactive_micro() -> Option<CompactionSpec> {
    Some(CompactionSpec {
        micro: true,
        keep_recent_turns: 1,
        ..CompactionSpec::new(400)
    })
}

/// At a turn's start the cheaper step runs first: clearing old results is
/// enough at turn 2, and at turn 3 the summary follows, reading the folded
/// part with its results cleared.
#[tokio::test]
async fn micro_compaction_runs_before_a_due_summary() {
    let stores = Arc::new(Stores::default());
    let provider = Window::new(never);
    let agent = agent_on(
        provider.clone(),
        proactive_micro(),
        &Runs::default(),
        stores.clone(),
    );
    let (run_id, events) = run(&agent, &stores).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
    let reports = compactions(&events);
    let strategies: Vec<_> = reports.iter().map(|c| c.strategy.as_str()).collect();
    assert_eq!(strategies, ["micro", "summary"], "{reports:#?}");
    assert!(reports.iter().all(|c| !c.forced));
    assert_eq!(provider.calls("summary"), 1);
    let summary_request = provider.summary_requests.lock().unwrap()[0].clone();
    assert!(
        big_results(&summary_request) <= 1,
        "the summary reads cleared results: {summary_request:#?}"
    );
    check_unique_deliveries(&events, "clean");
    let reasons: Vec<_> = (entries(&stores).await.into_iter())
        .filter_map(|e| match e {
            LogEntry::Projection(p) => Some(p.reason),
            _ => None,
        })
        .collect();
    assert_eq!(reasons, ["micro-compaction", "compaction"]);
}

#[tokio::test]
async fn a_crash_at_any_write_of_a_turn_start_micro_compaction_recovers() {
    crash_everywhere(proactive_micro(), || Window::new(never)).await;
}
