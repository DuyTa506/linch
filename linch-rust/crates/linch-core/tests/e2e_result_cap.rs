//! `spec.tool_result_cap`: a tool result over the cap shows its head and
//! tail in the view, the whole result stays in the log and the events, and
//! `ReadToolResult` reads the rest.

mod common;

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use common::*;
use futures::StreamExt;
use linch_core::compaction::CLEARED_RESULT;
use linch_core::events::kinds::CompactionEvent;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, CompactionSpec, ContentBlock, Envelope, Event, JsonObject,
    LogEntry, LoopGuard, Message, PermissionMode, Provider, ProviderError, ProviderEvent,
    ProviderRequest, ProviderStream, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider,
    ScriptedTurn, SessionId, SessionStore, Tool, ToolCache, ToolContext, ToolOutput, ToolRegistry,
    ToolResult, ToolResultCap, ToolResultPart, ToolScope,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// The cap of most tests: 2000 bytes of a result in the view.
const CAP: u64 = 500;
const BUDGET: usize = 2000;

/// A tool returning `len` bytes of pseudo-random letters, so a byte at the
/// wrong offset shows. With `vary`, the middle of the text (all but its
/// first and last 1000 bytes) differs on each call, the ends do not.
struct Blob {
    calls: Mutex<u32>,
}

fn letters(len: usize, seed: u32) -> String {
    let mut state = seed.wrapping_mul(2_654_435_761).wrapping_add(1);
    (0..len)
        .map(|_| {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            char::from(b'a' + ((state >> 24) % 26) as u8)
        })
        .collect()
}

fn blob_text(len: usize, middle_seed: u32) -> String {
    let (head, tail) = (letters(1000, 1), letters(1000, 2));
    format!("{head}{}{tail}", letters(len - 2000, middle_seed))
}

#[async_trait]
impl Tool for Blob {
    fn name(&self) -> &str {
        "Blob"
    }
    fn description(&self) -> &str {
        "A long text."
    }
    fn input_schema(&self) -> &JsonObject {
        static SCHEMA: std::sync::OnceLock<JsonObject> = std::sync::OnceLock::new();
        SCHEMA.get_or_init(|| json!({"type": "object"}).as_object().unwrap().clone())
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        let len = input.get("len").and_then(|v| v.as_u64()).unwrap_or(20_000) as usize;
        let seed = match input.get("vary").and_then(|v| v.as_bool()) {
            Some(true) => {
                let mut calls = self.calls.lock().unwrap();
                *calls += 1;
                *calls
            }
            _ => 0,
        };
        Ok(ToolOutput::new(json!(blob_text(len, seed))))
    }
}

fn blob() -> Blob {
    Blob {
        calls: Mutex::new(0),
    }
}

fn capped(cap: u64) -> AgentSpec {
    AgentSpec {
        tool_result_cap: Some(ToolResultCap::new(cap)),
        ..spec()
    }
}

fn tools(runs: &Runs) -> Arc<ToolRegistry> {
    let registry = registry(vec![Echo::new("Echo", ToolScope::Read, runs)]);
    let _ = registry.register(Arc::new(blob())).unwrap();
    registry
}

fn build<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(tools(runs))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

/// A response calling `tool` with `input`, id `call_{n}`.
fn call(n: usize, tool: &str, input: serde_json::Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text("working")
        .tool_call(&format!("call_{n}"), tool, &input)
        .usage(usage(10, 5))
        .build()
}

fn script(calls: usize, tool: &str, input: serde_json::Value) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = (0..calls).map(|n| call(n, tool, input.clone())).collect();
    turns.push(text_turn("done"));
    turns
}

/// The text of every tool result of `messages`, in order.
fn result_texts(messages: &[Message]) -> Vec<String> {
    let mut texts = Vec::new();
    for message in messages {
        for block in &message.content {
            if let ContentBlock::ToolResult(result) = block {
                let parts = result.content.iter().map(|part| match part {
                    ToolResultPart::Text(text) => text.text.as_str(),
                    ToolResultPart::Image(_) => "",
                });
                texts.push(parts.collect::<Vec<_>>().join("\n"));
            }
        }
    }
    texts
}

async fn entries(stores: &Stores) -> Vec<LogEntry> {
    stores
        .sessions
        .load(NS, &SessionId::new("s1"))
        .await
        .unwrap()
}

/// The messages of the session's `visible` or `historical` entries.
async fn log_view(stores: &Stores, visible: bool) -> Vec<Message> {
    let mut log = linch_core::SessionLog::new();
    for entry in entries(stores).await {
        match entry {
            LogEntry::Message(m) => log.append((*m.message).clone(), m.visible, m.historical),
            LogEntry::Projection(p) => {
                log.record_projection(p.replacement, p.reason);
            }
            LogEntry::Prefix(_) => {}
        }
    }
    let view = if visible {
        log.provider_view()
    } else {
        log.full_history()
    };
    view.iter().map(|m| (**m).clone()).collect()
}

async fn run(agent: &Agent) -> Vec<Envelope> {
    finish(agent.run("go", RunOptions::new("s1"))).await
}

fn ends(events: &[Envelope]) -> Vec<String> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::ToolCallEnd(end) => Some(
                result_texts(&[Message::new(
                    linch_core::Role::User,
                    vec![ContentBlock::ToolResult(end.result.clone())],
                )])[0]
                    .clone(),
            ),
            _ => None,
        })
        .collect()
}

fn guards(events: &[Envelope]) -> usize {
    let is_guard = |e: &&Envelope| matches!(e.event, Event::LoopGuard(_));
    events.iter().filter(is_guard).count()
}

// (a) A 200 KB result of a host tool is capped, and the request stays small.
#[tokio::test]
async fn a_big_host_result_is_capped_and_the_request_stays_bounded() {
    let stores = Arc::new(Stores::default());
    let provider = Arc::new(ScriptedProvider::new(script(
        6,
        "Echo",
        json!({"chars": 200_000}),
    )));
    let agent = build(
        capped(CAP),
        provider.clone(),
        &Runs::default(),
        stores.clone(),
    );
    let events = run(&agent).await;

    for (n, request) in provider.requests().iter().enumerate() {
        let texts = result_texts(&request.messages);
        assert_eq!(texts.len(), n);
        for text in &texts {
            assert!(text.len() <= BUDGET, "{}", text.len());
            assert!(
                text.contains("bytes omitted; read them with ReadToolResult("),
                "{text}"
            );
        }
        let size = serde_json::to_string(&request.messages).unwrap().len();
        assert!(size < 4000 + n * 3000, "request {n} is {size} bytes");
    }
    // The events and the audit projection keep every byte.
    assert!(ends(&events).iter().all(|text| text.len() == 200_001));
    let full = log_view(&stores, false).await;
    assert!(result_texts(&full).iter().all(|text| text.len() == 200_001));
    let shown = log_view(&stores, true).await;
    assert!(result_texts(&shown).iter().all(|text| text.len() <= BUDGET));
    // The log shows what the model got: capped entries are visible only,
    // whole ones historical only.
    let flags: Vec<_> = entries(&stores)
        .await
        .iter()
        .filter_map(|e| match e {
            LogEntry::Message(m) if m.message.role == linch_core::Role::User => {
                Some((m.visible, m.historical))
            }
            _ => None,
        })
        .collect();
    assert_eq!(flags[0], (true, true), "the prompt");
    assert_eq!(&flags[1..3], [(false, true), (true, false)]);
}

/// A model that reads a cut result to its end: it calls `Blob`, then follows
/// the offset in the last result it got until the result says it ended.
struct Pager {
    len: usize,
}

#[async_trait]
impl Provider for Pager {
    fn id(&self) -> &str {
        "pager"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let n = (request.messages.iter())
            .filter(|m| m.role == linch_core::Role::Assistant)
            .count();
        let last = result_texts(&request.messages).pop();
        let turn = match last {
            None => call(0, "Blob", json!({"len": self.len})),
            Some(text) if text.contains("end of the result") => text_turn("done"),
            Some(text) => {
                let at = text.rfind("offset=").expect("the result names an offset") + 7;
                let digits: String = text[at..]
                    .chars()
                    .take_while(char::is_ascii_digit)
                    .collect();
                let input =
                    json!({"tool_use_id": "call_0", "offset": digits.parse::<u64>().unwrap()});
                call(n, "ReadToolResult", input)
            }
        };
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            turn.events.into_iter().map(Ok).collect();
        Ok(futures::stream::iter(events).boxed())
    }
}

// (b) ReadToolResult returns the omitted middle by offset, and paging by
// the next offset reaches the end with no gap and no byte twice.
#[tokio::test]
async fn read_back_pages_through_the_omitted_part_to_the_end() {
    let len = 6000;
    let stores = Arc::new(Stores::default());
    let agent = build(
        capped(128),
        Arc::new(Pager { len }),
        &Runs::default(),
        stores.clone(),
    );
    let events = run(&agent).await;

    let texts = result_texts(&log_view(&stores, true).await);
    let body = blob_text(len, 0);
    let (first, pages) = texts.split_first().unwrap();
    let (head, rest) = first.split_once("\n[... ").unwrap();
    let (marker, tail) = rest.split_once(" ...]\n").unwrap();
    let at = head.len();
    assert!(marker.ends_with(&format!("offset={at})")), "{marker}");
    assert!(body.starts_with(head) && body.ends_with(tail));
    let mut got = String::from(head);
    for page in pages {
        assert!(!page.contains("bytes omitted"), "a page was cut again");
        assert!(page.len() <= 512, "{}", page.len());
        got.push_str(page.rsplit_once("\n[... ").unwrap().0);
    }
    assert_eq!(got, body);
    assert!(pages.last().unwrap().ends_with("end of the result ...]"));
    // Read back with every call an ordinary tool call.
    let reads = (events.iter())
        .filter(|e| matches!(&e.event, Event::ToolCallStart(s) if s.tool_name == "ReadToolResult"))
        .count();
    assert_eq!(reads, pages.len());
}

// (b') Only this session's calls can be read.
#[tokio::test]
async fn an_unknown_id_is_a_clear_error_the_model_sees() {
    let stores = Arc::new(Stores::default());
    let turns = vec![
        call(0, "ReadToolResult", json!({"tool_use_id": "never"})),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let agent = build(capped(CAP), provider.clone(), &Runs::default(), stores);
    run(&agent).await;
    let texts = result_texts(&provider.requests()[1].messages);
    assert_eq!(
        texts,
        ["no tool result with tool_use_id \"never\" in this session"]
    );
}

// (c) Results at or under the cap are byte-identical to a run with no cap.
#[tokio::test]
async fn under_cap_results_are_unchanged_byte_for_byte() {
    let mut seen = Vec::new();
    for spec in [spec(), capped(CAP)] {
        let stores = Arc::new(Stores::default());
        let script = script(3, "Echo", json!({"chars": 1990}));
        let provider = Arc::new(ScriptedProvider::new(script));
        let agent = build(spec, provider.clone(), &Runs::default(), stores.clone());
        let events = run(&agent).await;
        let requests: Vec<_> = (provider.requests().into_iter())
            .map(|r| serde_json::to_string(&r.messages).unwrap())
            .collect();
        let log = serde_json::to_string(&messages(&stores.sessions, "s1").await).unwrap();
        let kinds = kinds(&events);
        seen.push((requests, log, kinds, ends(&events)));
    }
    assert_eq!(seen[0], seen[1]);
}

// (d) The loop guard judges the whole result, not the cut view.
#[tokio::test]
async fn the_loop_guard_sees_the_whole_result() {
    let run_of = |input: serde_json::Value| async move {
        let stores = Arc::new(Stores::default());
        let provider = Arc::new(ScriptedProvider::new(script(5, "Blob", input)));
        // A cap whose tail stays inside the part the results share.
        let agent = build(
            capped(128),
            provider.clone(),
            &Runs::default(),
            stores.clone(),
        );
        let events = run(&agent).await;
        let status = (stores.runs.load(NS, &events[0].run_id).await.unwrap()).status;
        (events, provider, status)
    };
    // The same call with the same result three times: a loop.
    let (events, _, status) = run_of(json!({})).await;
    assert_eq!((guards(&events), status), (1, RunStatus::Failed));
    // Results that differ only in the middle look the same in the view and
    // are not a loop.
    let (events, provider, status) = run_of(json!({"vary": true})).await;
    assert_eq!((guards(&events), status), (0, RunStatus::Completed));
    let last = provider.requests().pop().unwrap();
    let texts = result_texts(&last.messages);
    assert_eq!(texts.len(), 5);
    // The marker names each call's own id; the rest is the same.
    let unnamed: Vec<_> = (texts.iter().enumerate())
        .map(|(n, text)| text.replace(&format!("call_{n}"), "call_N"))
        .collect();
    assert!(
        unnamed.windows(2).all(|pair| pair[0] == pair[1]),
        "the view differs"
    );
    let whole = ends(&events);
    assert!(
        whole.windows(2).all(|pair| pair[0] != pair[1]),
        "the results do not"
    );
}

// (g) A cache hit stores and serves the whole result; the view is cut again
// the same way.
#[tokio::test]
async fn a_cache_hit_serves_the_whole_result_and_the_same_view() {
    let mut spec = capped(CAP);
    spec.tool_cache = Some(ToolCache::new(["Echo"]));
    spec.loop_guard = LoopGuard {
        max_identical_tool_calls: 0,
        ..LoopGuard::default()
    };
    let (stores, runs) = (Arc::new(Stores::default()), Runs::default());
    let script = script(2, "Echo", json!({"chars": 50_000}));
    let provider = Arc::new(ScriptedProvider::new(script));
    let agent = build(spec, provider.clone(), &runs, stores.clone());
    let events = run(&agent).await;

    let hits: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::ToolCallEnd(end) => Some((end.cache_hit, end.result.content.clone())),
            _ => None,
        })
        .collect();
    assert_eq!(hits.len(), 2);
    assert_eq!((hits[0].0, hits[1].0), (false, true));
    assert_eq!(hits[0].1, hits[1].1);
    assert_eq!(ends(&events)[1].len(), 50_001);
    let texts = result_texts(&provider.requests().pop().unwrap().messages);
    assert_eq!(texts.len(), 2);
    assert_eq!(texts[0].replace("call_0", "call_1"), texts[1]);
}

// (h) Compaction measures the view the model gets: results that are cut do
// not make a trigger fire that the whole results would.
#[tokio::test]
async fn compaction_measures_the_capped_view() {
    let run_with = |cap: Option<ToolResultCap>| async move {
        let mut spec = AgentSpec {
            tool_result_cap: cap,
            compaction: Some(CompactionSpec {
                keep_recent_turns: 1,
                ..CompactionSpec::new(20_000)
            }),
            ..spec()
        };
        spec.max_turns = Some(20);
        let stores = Arc::new(Stores::default());
        let script = script(4, "Echo", json!({"chars": 100_000}));
        let provider = Arc::new(ScriptedProvider::new(script));
        let agent = build(spec, provider, &Runs::default(), stores);
        run(&agent).await
    };
    let compactions = |events: &[Envelope]| {
        (events.iter())
            .filter(|e| matches!(e.event, Event::Compaction(_)))
            .count()
    };
    assert_eq!(
        compactions(&run_with(Some(ToolResultCap::new(CAP))).await),
        0
    );
    // Without the cap, 25k tokens of one result is over the trigger: the
    // run asks for a summary (the script has no turn for it, so it ends
    // however it ends; the compaction event is the point).
    assert!(compactions(&run_with(None).await) > 0);
}

// Micro-compaction works on the capped view.
#[tokio::test]
async fn micro_compaction_clears_capped_results() {
    let mut spec = capped(128);
    spec.compaction = Some(CompactionSpec {
        micro: true,
        keep_recent_turns: 1,
        ..CompactionSpec::new(700)
    });
    let stores = Arc::new(Stores::default());
    // Calls that differ, so the loop guard has no repeat to stop.
    let mut turns: Vec<_> = (0..8)
        .map(|n| call(n, "Echo", json!({"chars": 20_000, "n": n})))
        .collect();
    turns.push(text_turn("done"));
    let provider = Arc::new(Replay::new(turns));
    let agent = build(spec, provider.clone(), &Runs::default(), stores.clone());
    let events = run(&agent).await;
    let micro: Vec<_> = (events.iter())
        .filter_map(|e| match &e.event {
            Event::Compaction(c) => Some(c.clone()),
            _ => None,
        })
        .collect();
    let strategies: Vec<&str> = micro
        .iter()
        .map(|c: &CompactionEvent| c.strategy.as_str())
        .collect();
    assert!(
        !strategies.is_empty() && strategies.iter().all(|s| *s == "micro"),
        "{strategies:?}"
    );
    let texts = result_texts(&log_view(&stores, true).await);
    assert!(texts.iter().any(|t| t == CLEARED_RESULT), "{texts:?}");
}

/// The view of a session, with the tool results' bytes.
fn view_json(view: &[Message]) -> String {
    serde_json::to_string(view).unwrap()
}

// (e) A crash at any write of a capped run resumes to the view of the clean
// run, byte for byte, with each tool run once.
#[tokio::test]
async fn a_crash_at_any_write_resumes_to_the_same_capped_view() {
    let turns = || script(2, "Echo", json!({"chars": 30_000}));
    let clean = Arc::new(Stores::default());
    let agent = build(
        capped(CAP),
        Arc::new(Replay::new(turns())),
        &Runs::default(),
        clean.clone(),
    );
    let clean_events = run(&agent).await;
    let want = view_json(&log_view(&clean, true).await);
    let want_full = view_json(&log_view(&clean, false).await);
    assert!(want.len() < 8000 && want_full.len() > 60_000);
    let writes = clean.log.lock().unwrap().len() as u64;
    assert!(!clean_events.is_empty() && writes > 0);

    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let runs = Runs::default();
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let provider = Arc::new(Replay::new(turns()));
            let first = build(capped(CAP), provider.clone(), &runs, faulty);
            let stream = first.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let second = build(capped(CAP), provider, &runs, stores.clone());
            let run_id = recover(&second, &stores, run_id).await;
            let record = stores.runs.load(NS, &run_id).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            // A crash on a tool barrier may leave the short interrupted
            // placeholder in place of a result.
            let barrier = clean.log.lock().unwrap()[n as usize - 1].is_tool_barrier();
            if !barrier {
                assert_eq!(view_json(&log_view(&stores, true).await), want, "{context}");
                assert_eq!(
                    view_json(&log_view(&stores, false).await),
                    want_full,
                    "{context}"
                );
            }
            assert!(runs.lock().unwrap().values().all(|&n| n == 1), "{context}");
        }
    }
}

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

// A cap that cannot hold its own marker does not build.
#[tokio::test]
async fn a_cap_too_small_for_its_marker_does_not_build() {
    let stores = Arc::new(Stores::default());
    let spec = AgentSpec {
        tool_result_cap: Some(ToolResultCap::new(10)),
        ..spec()
    };
    let result = AgentBuilder::new(spec)
        .provider(Arc::new(ScriptedProvider::new([])))
        .tools(registry(vec![]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build();
    assert!(
        matches!(result, Err(linch_core::BuildError::Invalid(r)) if r.contains("tool_result_cap"))
    );
}

// Off, the agent offers no read-back tool and its requests are what they
// were; on, the tool is offered.
#[tokio::test]
async fn the_read_back_tool_is_offered_only_with_the_cap() {
    for (spec, offered) in [(spec(), false), (capped(CAP), true)] {
        let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
        let agent = build(
            spec,
            provider.clone(),
            &Runs::default(),
            Arc::new(Stores::default()),
        );
        run(&agent).await;
        let names: Vec<_> = (provider.requests()[0].tools.iter())
            .map(|t| t.name.clone())
            .collect();
        assert_eq!(
            names.iter().any(|n| n == "ReadToolResult"),
            offered,
            "{names:?}"
        );
    }
}

/// How the view grows over 30 turns of tool results of 2, 12, 60 and 200 KB
/// (a mix of small reads, a file, a grep and a log): the estimated tokens of
/// the last request, and of all 30 requests together (what the run sends).
/// Printed for the PR; the bounds are what the cap promises.
#[tokio::test]
async fn the_view_grows_by_at_most_the_cap_a_turn() {
    let sizes = [2_000, 12_000, 60_000, 200_000];
    let mut seen = Vec::new();
    for cap in [None, Some(8000), Some(4000)] {
        let spec = AgentSpec {
            tool_result_cap: cap.map(ToolResultCap::new),
            ..spec()
        };
        let mut turns: Vec<_> = (0..30)
            .map(|n| call(n, "Echo", json!({"chars": sizes[n % 4], "n": n})))
            .collect();
        turns.push(text_turn("done"));
        let provider = Arc::new(ScriptedProvider::new(turns));
        let agent = build(
            spec,
            provider.clone(),
            &Runs::default(),
            Arc::new(Stores::default()),
        );
        run(&agent).await;
        let tokens: Vec<u64> = (provider.requests().iter())
            .map(|r| serde_json::to_string(&r.messages).unwrap().len() as u64 / 4)
            .collect();
        seen.push((cap, *tokens.last().unwrap(), tokens.iter().sum::<u64>()));
    }
    for (cap, last, total) in &seen {
        eprintln!("cap {cap:?}: last request ~{last} tokens, 30 requests ~{total} tokens");
    }
    let (none, eight, four) = (seen[0], seen[1], seen[2]);
    // Each turn adds at most the cap (and its marker, call and JSON framing).
    assert!(eight.1 <= 30 * (8000 + 150), "{eight:?}");
    assert!(four.1 <= 30 * (4000 + 150), "{four:?}");
    assert!(four.1 < eight.1 && eight.1 < none.1);
}
