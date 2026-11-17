//! Compaction in a run: it triggers past the threshold, logs a projection,
//! keeps the cached prefix byte-identical, pays each summary once, and
//! recovers from a crash at every write.

mod common;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::hooks::hook;
use common::*;
use futures::StreamExt;
use linch_core::events::kinds::CompactionEvent;
use linch_core::events::model_call::{CallDisposition, ModelCallKind};
use linch_core::hooks::{PostCompact, PostCompactRequest, PreCompact, PreCompactRequest};
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, CompactionSpec, ContentBlock, Envelope, Event, Hooks, LogEntry, Message,
    PermissionMode, Provider, ProviderError, ProviderRequest, ProviderStream, Role, RunBudget,
    RunId, RunOptions, RunStatus, RunStore, SessionId, SessionLog, SessionStore, ToolScope, Usage,
};
use serde_json::json;
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
    /// The context window the provider reports.
    window: u64,
    /// What a summary request is answered with.
    summary: Summary,
}

/// A reply to a summary request.
#[derive(Clone, Copy)]
enum Summary {
    /// `summary of {n} messages`, which is smaller than what it folds.
    Fits,
    /// Far bigger than the part it would replace.
    Long,
    /// Only whitespace.
    Blank,
    /// A call of `Read`, as a model that ignores "do not call any tools".
    Tool,
}

impl Keyed {
    fn new() -> Arc<Self> {
        Arc::new(Self::answering())
    }

    fn answering() -> Self {
        Self {
            turns: big_script(3),
            calls: Mutex::default(),
            requests: Mutex::default(),
            failing_summaries: false,
            window: 128_000,
            summary: Summary::Fits,
        }
    }
}

/// `n` turns that each call `Read` once with an input far bigger than a
/// summary, then an answer: a summary smaller than what it folds is applied.
fn big_script(n: usize) -> Vec<linch_core::ScriptedTurn> {
    let mut turns: Vec<_> = (0..n)
        .map(|t| {
            let value = serde_json::json!({"value": format!("v{t}{}", "x".repeat(400))});
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

#[async_trait]
impl Provider for Keyed {
    fn id(&self) -> &str {
        "keyed"
    }

    fn context_window(&self, _model: &str) -> u64 {
        self.window
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.requests.lock().unwrap().push(request.clone());
        let summarizing = is_summary_call(&request);
        if summarizing && self.failing_summaries {
            return Err(ProviderError::Status {
                status: 503,
                retry_after: None,
                message: "busy".into(),
            });
        }
        let (key, turn) = if summarizing {
            let n = request.messages.len();
            let builder = TurnBuilder::new(MODEL).usage(usage(3, 2));
            let summary = match self.summary {
                Summary::Fits => builder.text(format!("summary of {n} messages")),
                Summary::Long => builder.text("y".repeat(8000)),
                Summary::Blank => builder.text("  \n "),
                Summary::Tool => builder.tool_call("call_s_0", "Read", &json!({"value": "x"})),
            };
            (format!("summary:{n}"), summary.build())
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
            LogEntry::Prefix(p) => log.record_prefix(p),
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
    let summary = Message::user_text("<summary of earlier conversation>\n\nsummary of 7 messages");
    assert_eq!(compacted[..2], [Message::user_text("go"), summary]);
    assert_eq!(
        compacted.len(),
        5,
        "the first message, the summary, the last turn"
    );

    let requests = provider.requests.lock().unwrap().clone();
    let (summaries, primaries): (Vec<_>, Vec<_>) =
        (requests.iter()).partition(|r| is_summary_call(r));
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
    // The summary call is the last primary call's request, kept as it was
    // (system blocks, tools, tool choice, messages), with the view's newer
    // messages and the instruction after it: the whole view, not only the
    // folded part, so a prompt cache of that call serves it.
    let earlier = primaries[1];
    assert_eq!(summaries[0].system, earlier.system);
    assert_eq!(summaries[0].tools, earlier.tools);
    assert_eq!(summaries[0].tool_choice, earlier.tool_choice);
    let shared = earlier.messages.len();
    assert_eq!(summaries[0].messages[..shared], earlier.messages[..]);
    assert_eq!(
        summaries[0].messages.len(),
        shared + 3,
        "the last turn and its result, then the instruction"
    );

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

/// `pre_compact` fires once per compacting turn, before the summary call,
/// with the view as it stood; `post_compact` fires once it replaced the
/// view with a summary, with the before/after counts.
#[tokio::test]
async fn pre_compact_and_post_compact_bracket_a_successful_compaction() {
    let stores = Arc::new(Stores::default());
    let provider = Keyed::new();
    let runs = Runs::default();

    let pre: Arc<Mutex<Vec<PreCompactRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let post: Arc<Mutex<Vec<PostCompactRequest>>> = Arc::new(Mutex::new(Vec::new()));
    let hooks = Hooks::default();
    let log_pre = Arc::clone(&pre);
    let on_pre = hook::<PreCompact>("watch", move |request| {
        log_pre.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.pre_compact.register(on_pre.arc()).unwrap();
    let log_post = Arc::clone(&post);
    let on_post = hook::<PostCompact>("watch", move |request| {
        log_post.lock().unwrap().push(request.clone());
        Ok(())
    });
    let _ = hooks.post_compact.register(on_post.arc()).unwrap();

    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let agent = AgentBuilder::new(spec)
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .hooks(Arc::new(hooks))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let _ = finish(stream).await;

    let pre = pre.lock().unwrap();
    let post = post.lock().unwrap();
    assert_eq!(pre.len(), 2, "turns 2 and 3 compact");
    assert_eq!(post.len(), 2);
    for (pre, post) in pre.iter().zip(post.iter()) {
        assert_eq!(pre.run_id, run_id);
        assert_eq!(post.run_id, run_id);
        assert_eq!(pre.turn, post.turn);
        assert_eq!(pre.strategy, "summary");
        assert_eq!(post.strategy, "summary");
        assert_eq!(pre.messages, post.messages_before);
        assert_eq!(pre.tokens, post.tokens_before);
        assert!(post.messages_after < post.messages_before);
    }
}

/// A spent budget refuses the summary call as well as the primary one.
#[tokio::test]
async fn a_spent_budget_makes_no_summary_call() {
    let stores = Arc::new(Stores::default());
    let provider = Keyed::new();
    let runs = Runs::default();
    let mut spec = compacting_agent(provider.clone(), &runs, stores.clone())
        .spec()
        .clone();
    // Two primary calls spend 30; the third turn would compact first.
    spec.budget = Some(RunBudget {
        max_tokens: Some(30),
        ..RunBudget::default()
    });
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let agent = agent_with(
        spec,
        provider.clone(),
        tools,
        policy(PermissionMode::SkipDangerous),
        stores,
    );
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let calls = provider.calls.lock().unwrap().clone();
    assert!(
        calls.keys().all(|key| key.starts_with("primary")),
        "{calls:?}"
    );
    assert_eq!(calls.len(), 2, "{calls:?}");
    let error = (envelopes.iter()).find_map(|e| match &e.event {
        Event::Error(error) => Some(error.error["name"].clone()),
        _ => None,
    });
    assert_eq!(error, Some("BudgetExceededError".into()));
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
        assert!(!log.iter().any(|e| matches!(e, LogEntry::Projection(_))));
        assert_eq!(
            provider.requests.lock().unwrap().len(),
            4,
            "no summary call"
        );
        assert_eq!(stores.runs.writes().commit, 18);
    }
}

#[tokio::test]
async fn without_a_threshold_compaction_follows_the_context_window() {
    for (window, summaries) in [(128_000, 0), (1, 2)] {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Keyed {
            window,
            ..Keyed::answering()
        });
        let tools = registry(vec![Echo::new("Read", ToolScope::Read, &Runs::default())]);
        let mut spec = spec();
        spec.compaction = Some(CompactionSpec {
            keep_recent_turns: 1,
            ..CompactionSpec::default()
        });
        let permissions = policy(PermissionMode::SkipDangerous);
        let agent = agent_with(spec, provider.clone(), tools, permissions, stores.clone());
        finish(agent.run("go", RunOptions::new("s1"))).await;
        let requests = provider.requests.lock().unwrap();
        let summarizing = |r: &&ProviderRequest| is_summary_call(r);
        let made = requests.iter().filter(summarizing).count();
        assert_eq!(made, summaries, "window {window}");
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
    assert!(!log.iter().any(|e| matches!(e, LogEntry::Projection(_))));
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
        result.usage_complete,
        "summaries refused with an error status cost nothing"
    );
    // A failed summary costs two writes: its delivery, then its receipt.
    assert_eq!(stores.runs.writes().commit, 18 + 2 * 2);
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
/// call was settled by an abandoned receipt of its own kind.
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
    // An abandoned primary call never excuses a second summary (R1-core 2).
    for (key, kind, logical) in [("summary", ":compaction:", 2), ("primary", ":primary:", 4)] {
        let calls = provider.calls.lock().unwrap().clone().into_iter();
        let calls: u32 = calls
            .filter(|(k, _)| k.starts_with(key))
            .map(|(_, n)| n)
            .sum();
        let spare = abandoned.iter().filter(|(id, _)| id.contains(kind));
        let spare: u32 = spare.map(|(_, n)| n).sum();
        assert!(
            calls <= logical + spare,
            "{context}: {key} {calls} calls, {spare} abandoned"
        );
    }
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

/// A summary is model output the model reads back, so the spec's response
/// redaction scrubs it before it is logged.
#[tokio::test]
async fn a_summary_is_redacted_before_it_is_logged() {
    use linch_core::redaction::{RedactionRule, RedactionSpec};
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    spec.redaction = Some(RedactionSpec {
        rules: vec![RedactionRule {
            pattern: r"summary of \d+ messages".into(),
            replacement: "[SUMMARY]".into(),
        }],
        response: true,
        tool_results: false,
        prompt: false,
    });
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec, Keyed::new(), tools, permissions, stores.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let compacted = view(entries(&stores).await);
    let summary = Message::user_text("<summary of earlier conversation>\n\n[SUMMARY]");
    assert_eq!(compacted[1], summary);
}

/// A reply that is not a summary leaves the view as it was, once per turn
/// that was due, and the run goes on: each is a rejected receipt and a
/// `compaction` event that says why.
#[tokio::test]
async fn a_reply_that_is_not_a_summary_is_rejected_and_the_run_goes_on() {
    for (summary, why) in [
        (Summary::Long, "not_smaller"),
        (Summary::Blank, "empty"),
        (Summary::Tool, "tool_call"),
    ] {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Keyed {
            summary,
            ..Keyed::answering()
        });
        let agent = compacting_agent(provider.clone(), &Runs::default(), stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        let envelopes = finish(stream).await;
        assert_eq!(
            status(&stores, &run_id).await,
            RunStatus::Completed,
            "{why}"
        );
        let log = entries(&stores).await;
        assert!(!log.iter().any(|e| matches!(e, LogEntry::Projection(_))));
        let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
        let rejected = (events.iter())
            .filter(|e| {
                matches!(&e.event, Event::ModelCallReceipt(r)
                if r.kind == ModelCallKind::Compaction && r.disposition == CallDisposition::Rejected)
            })
            .count();
        assert_eq!(rejected, 2, "{why}: each due turn asked once, no retry");
        let calls: u32 = (provider.calls.lock().unwrap().iter())
            .filter(|(key, _)| key.starts_with("summary"))
            .map(|(_, n)| n)
            .sum();
        assert_eq!(calls, 2, "{why}");
        let reports: Vec<_> = (envelopes.iter())
            .filter_map(|e| match &e.event {
                Event::Compaction(c) => Some(c),
                _ => None,
            })
            .collect();
        assert_eq!(reports.len(), 2, "{why}");
        for (n, report) in reports.into_iter().enumerate() {
            let reason = report.rejected.as_deref().unwrap();
            match n {
                0 => assert_eq!(reason, format!("{why}: the next due turn asks again")),
                // The second in a row starts the hold.
                _ => assert!(
                    reason.starts_with(&format!("{why}: asks again once the view has grown")),
                    "{why}: {reason}"
                ),
            }
            assert_eq!(report.messages_before, report.messages_after);
            assert_eq!(report.tokens_before, report.tokens_after);
        }
    }
}

/// A window below `keep_user_tokens` plus the summary's output, the default
/// spec (the trigger follows the window): every accepted summary leaves a
/// view smaller than the one it replaced and under the window.
#[tokio::test]
async fn on_a_tiny_window_a_compaction_never_leaves_a_view_over_it() {
    let stores = Arc::new(Stores::default());
    let window = 3000;
    let provider = Arc::new(Keyed {
        turns: big_script(12),
        window,
        ..Keyed::answering()
    });
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::default()
    });
    assert!(
        window < 15_000 + 4096,
        "below keep_user_tokens + max_output"
    );
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Read", ToolScope::Read, &runs)]);
    let permissions = policy(PermissionMode::SkipDangerous);
    let agent = agent_with(spec, provider, tools, permissions, stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
    let accepted: Vec<_> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Compaction(c) if c.rejected.is_none() => Some(c),
            _ => None,
        })
        .collect();
    assert!(!accepted.is_empty(), "the window forced compactions");
    for report in accepted {
        assert!(report.tokens_after < report.tokens_before, "{report:?}");
        assert!(report.tokens_after < window, "{report:?}");
    }
}

/// A model that never gives a usable summary must not be paid for one on
/// every turn of a long run: a rejected summary is asked again only after
/// a backoff or up to a cap (the number is the fix's choice; it must be
/// well under the 10 due turns).
#[tokio::test]
async fn a_summary_that_is_always_rejected_is_not_bought_every_turn() {
    for summary in [Summary::Tool, Summary::Long, Summary::Blank] {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(Keyed {
            turns: big_script(10),
            summary,
            ..Keyed::answering()
        });
        let agent = compacting_agent(provider.clone(), &Runs::default(), stores.clone());
        let stream = agent.run("go", RunOptions::new("s1"));
        let run_id = stream.run_id().clone();
        finish(stream).await;
        assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
        let calls: u32 = (provider.calls.lock().unwrap().iter())
            .filter(|(key, _)| key.starts_with("summary"))
            .map(|(_, n)| n)
            .sum();
        assert!(
            calls <= 3,
            "{calls} summary calls in 10 turns, all rejected"
        );
    }
}

/// The hold on a rejected summary is read back from the durable events: a
/// run that crashes after its second rejection and resumes does not go back
/// to asking at every due turn.
#[tokio::test]
async fn a_crash_does_not_lift_the_hold_on_a_rejected_summary() {
    let script = || {
        Arc::new(Keyed {
            turns: big_script(10),
            summary: Summary::Tool,
            ..Keyed::answering()
        })
    };
    let clean = Arc::new(Stores::default());
    let agent = compacting_agent(script(), &Runs::default(), clean.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let provider = script();
            let runs = Runs::default();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream =
                compacting_agent(provider.clone(), &runs, faulty).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let resumed = compacting_agent(provider.clone(), &runs, stores.clone());
            let run_id = recover(&resumed, &stores, run_id).await;
            assert_eq!(status(&stores, &run_id).await, RunStatus::Completed);
            let calls: u32 = (provider.calls.lock().unwrap().iter())
                .filter(|(key, _)| key.starts_with("summary"))
                .map(|(_, n)| n)
                .sum();
            // Two asks, and one more when the crash lost an answer.
            assert!(calls <= 3, "{context}: {calls} summary calls");
        }
    }
}

async fn status(stores: &Stores, run_id: &RunId) -> RunStatus {
    stores.runs.load(NS, run_id).await.unwrap().status
}
