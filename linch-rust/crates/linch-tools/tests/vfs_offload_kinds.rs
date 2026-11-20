//! Offload inside real runs, for every kind of long result: a structured
//! value, an error and one huge line. The model sees a pointer, the backend
//! holds the payload, and `read_file` pages it back.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, ContentBlock, Event, Hooks, JsonObject, JsonValue,
    MemoryRunStore, MemorySessionStore, PermissionEngine, PermissionMode, PermissionPolicy,
    Provider, ProviderError, ProviderEvent, ProviderRequest, ProviderStream, Role, RunId,
    RunOptions, RunStore, ScriptedProvider, ScriptedTurn, SessionId, SessionStore, StoreError,
    Tool, ToolContext, ToolOutput, ToolOutputError, ToolRegistry, ToolResult, ToolResultPart,
    ToolScope,
};
use linch_tools::vfs::{
    MemorySessions, OffloadConfig, ResultOffload, SessionFiles, offload_path, vfs_tools,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

const MODEL: &str = "scripted-model";
const NS: &str = "ns";
const THRESHOLD: u64 = 1_000;
/// The most bytes of one line `read_file` returns in one call.
const PAGE: usize = 16 * 1024;

/// A tool called `name` that answers every call with `result`.
struct Fixed {
    name: &'static str,
    result: ToolResult,
    runs: Arc<AtomicUsize>,
    schema: JsonObject,
}

impl Fixed {
    fn new(name: &'static str, result: ToolResult) -> (Self, Arc<AtomicUsize>) {
        let runs = Arc::new(AtomicUsize::new(0));
        let schema = json!({"type": "object"}).as_object().unwrap().clone();
        let tool = Self {
            name,
            result,
            runs: runs.clone(),
            schema,
        };
        (tool, runs)
    }
}

#[async_trait]
impl Tool for Fixed {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Return a fixed result."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, _input: JsonObject, _ctx: ToolContext) -> ToolResult {
        self.runs.fetch_add(1, Ordering::SeqCst);
        self.result.clone()
    }
}

/// One tool call from `name`, then the given `read_file` inputs one per
/// turn, then the answer.
fn turns(name: &str, reads: &[JsonValue]) -> Vec<ScriptedTurn> {
    let mut all = vec![
        TurnBuilder::new(MODEL)
            .tool_call("call_1", name, &json!({}))
            .build(),
    ];
    for (n, read) in reads.iter().enumerate() {
        let id = format!("read_{n}");
        all.push(
            TurnBuilder::new(MODEL)
                .tool_call(&id, "read_file", read)
                .build(),
        );
    }
    all.push(TurnBuilder::new(MODEL).text("done").build());
    all
}

fn registry(files: Arc<dyn SessionFiles>, tool: Fixed) -> Arc<ToolRegistry> {
    let tools = ToolRegistry::new();
    // Dropping a handle keeps the registration.
    let _ = tools.register(Arc::new(tool)).unwrap();
    for vfs in vfs_tools(files) {
        let _ = tools.register(vfs).unwrap();
    }
    Arc::new(tools)
}

fn hooks(files: Arc<dyn SessionFiles>, threshold: Option<u64>) -> Arc<Hooks> {
    let hooks = Hooks::default();
    if let Some(threshold) = threshold {
        let offload = ResultOffload::new(files, OffloadConfig::new(threshold)).unwrap();
        let _ = hooks.post_tool_use.register(Arc::new(offload)).unwrap();
    }
    Arc::new(hooks)
}

fn permissions() -> PermissionEngine {
    PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/")).unwrap()
}

/// What one run left behind.
struct Run {
    sessions: Arc<MemorySessions>,
    provider: Arc<ScriptedProvider>,
    events: Vec<linch_core::Envelope>,
    log: String,
}

/// Run `script` against a tool answering `result`, offloading above
/// `threshold` (or never).
async fn run(
    result: ToolResult,
    reads: &[JsonValue],
    threshold: Option<u64>,
    tool_name: &'static str,
) -> Run {
    let provider = Arc::new(ScriptedProvider::new(turns(tool_name, reads)));
    let sessions = Arc::new(MemorySessions::new());
    let files: Arc<dyn SessionFiles> = sessions.clone();
    let (tool, _runs) = Fixed::new(tool_name, result);
    let store = Arc::new(MemorySessionStore::new());
    let agent = AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider.clone())
        .tools(registry(files.clone(), tool))
        .hooks(hooks(files, threshold))
        .permissions(permissions())
        .run_store(Arc::new(MemoryRunStore::new()))
        .session_store(store.clone())
        .namespace(NS)
        .build()
        .unwrap();
    let session = SessionId::from("s1");
    let mut stream = agent.run("go", RunOptions::new(session.clone()));
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.expect("the run succeeds"));
    }
    let log = format!("{:?}", store.load(NS, &session).await.unwrap());
    Run {
        sessions,
        provider,
        events,
        log,
    }
}

/// `(tool_use_id, is_error, text)` of every tool result in `request`.
fn results(request: &ProviderRequest) -> Vec<(String, bool, String)> {
    let mut found = Vec::new();
    for message in &request.messages {
        for block in &message.content {
            let ContentBlock::ToolResult(result) = block else {
                continue;
            };
            let text: String = (result.content.iter())
                .filter_map(|part| match part {
                    ToolResultPart::Text(text) => Some(text.text.as_str()),
                    _ => None,
                })
                .collect();
            found.push((result.tool_use_id.clone(), result.is_error, text));
        }
    }
    found
}

/// The text of the result of call `id`, as the model sees it in the last
/// request.
fn seen(run: &Run, id: &str) -> (bool, String) {
    let request = run.provider.requests().pop().expect("a request");
    let (_, is_error, text) = results(&request)
        .into_iter()
        .find(|(call, _, _)| call == id)
        .unwrap_or_else(|| panic!("no result for {id}"));
    (is_error, text)
}

fn rows(n: usize) -> JsonValue {
    let rows: Vec<JsonValue> = (0..n)
        .map(|i| json!({"id": i, "name": format!("row {i}"), "tags": ["a", "b"]}))
        .collect();
    json!({"rows": rows, "total": n})
}

fn tool_end(run: &Run, id: &str) -> linch_core::events::kinds::ToolCallEndEvent {
    run.events
        .iter()
        .find_map(|envelope| match &envelope.event {
            Event::ToolCallEnd(end) if end.result.tool_use_id == id => Some(end.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no tool end for {id}"))
}

/// Where the offload of `rows(6_500)` by call `call_1` of session `s1` goes.
fn rows_path() -> String {
    let pretty = serde_json::to_string_pretty(&rows(6_500)).unwrap();
    offload_path("/offload", "s1", "rows", "call_1", &pretty)
}

#[tokio::test]
async fn a_structured_value_is_offloaded_and_paged_back_by_lines() {
    let value = rows(6_500);
    let compact = serde_json::to_string(&value).unwrap();
    assert!(compact.len() > 300_000);
    let pretty = serde_json::to_string_pretty(&value).unwrap();
    let all: Vec<&str> = pretty.split('\n').collect();
    let path = offload_path("/offload", "s1", "rows", "call_1", &pretty);
    let path = path.as_str();
    let reads = [
        json!({"path": path, "offset": 1, "limit": 4}),
        json!({"path": path, "offset": 20_001, "limit": 3}),
    ];
    let result = run(Ok(ToolOutput::new(value)), &reads, Some(THRESHOLD), "rows").await;

    for request in result.provider.requests().iter().skip(1) {
        let size = serde_json::to_string(request).unwrap().len();
        assert!(size < 20_000, "the request holds {size} bytes");
    }
    let stored = result
        .sessions
        .backend(&SessionId::from("s1"))
        .read(path)
        .await;
    assert_eq!(stored.unwrap(), pretty, "the file holds the pretty JSON");
    let marker = "row 6400";
    assert!(!format!("{:?}", result.events).contains(marker));
    assert!(!result.log.contains(marker), "the log holds the pointer");
    assert!(result.log.contains(&format!("offloaded to '{path}'")));

    let requests = result.provider.requests();
    let first = results(&requests[2]).pop().unwrap().2;
    assert_eq!(first, all[..4].join("\n"));
    let deep = results(&requests[3]).pop().unwrap().2;
    assert_eq!(deep, all[20_000..20_003].join("\n"));
    let end = tool_end(&result, "call_1");
    let metadata = end.metadata.expect("the host sees the sizes");
    assert_eq!(metadata["original_kind"], "json");
    assert_eq!(metadata["offloaded_to"], path);
    assert_eq!(metadata["original_bytes"], compact.len() as u64);
}

#[tokio::test]
async fn a_huge_error_is_offloaded_and_keeps_its_classification() {
    let message = format!(
        "FAILED\n{}",
        "assertion failed: left == right\n".repeat(10_000)
    );
    assert!(message.len() > 300_000);
    let mut error = ToolOutputError::new(message.clone());
    error.code = Some("exit_status".into());
    let path = offload_path("/offload", "s1", "test_run", "call_1", &message);
    let path = path.as_str();
    let reads = [json!({"path": path, "offset": 1, "limit": 2})];
    let on = run(Err(error.clone()), &reads, Some(THRESHOLD), "test_run").await;
    let off = run(Err(error), &reads, None, "test_run").await;

    let (is_error, shown) = seen(&on, "call_1");
    assert!(is_error, "still an error result");
    assert!(shown.starts_with("FAILED\nassertion failed"));
    assert!(shown.len() < 4_000, "the model sees {} bytes", shown.len());
    assert!(shown.contains(&format!("offloaded to '{path}'")));
    let stored = on.sessions.backend(&SessionId::from("s1")).read(path).await;
    assert_eq!(stored.unwrap(), message);
    let (on_end, off_end) = (tool_end(&on, "call_1"), tool_end(&off, "call_1"));
    assert_eq!(on_end.error_code.as_deref(), Some("exit_status"));
    assert_eq!(
        on_end.error_code, off_end.error_code,
        "the code is unchanged"
    );
    assert!(on_end.result.is_error && off_end.result.is_error);
    assert_eq!(on_end.metadata.unwrap()["offloaded_to"], path);
    let requests = on.provider.requests();
    let read = results(&requests[2]);
    assert_eq!(
        read.last().unwrap().2,
        "FAILED\nassertion failed: left == right"
    );
    assert!(
        on.log.len() < 20_000,
        "the log holds {} bytes",
        on.log.len()
    );
}

#[tokio::test]
async fn one_huge_line_is_paged_to_its_end_by_byte_offset() {
    let line: String = (0..500_000usize)
        .map(|i| char::from(b'a' + (i % 26) as u8))
        .collect();
    let path = offload_path("/offload", "s1", "minified", "call_1", &line);
    let path = path.as_str();
    let pages = line.len().div_ceil(PAGE);
    let reads: Vec<JsonValue> = (0..pages)
        .map(|n| match n {
            0 => json!({"path": path, "offset": 1, "limit": 1}),
            n => json!({"path": path, "offset": 1, "limit": 1, "byte_offset": n * PAGE}),
        })
        .collect();
    let result = run(
        Ok(ToolOutput::new(line.clone())),
        &reads,
        Some(THRESHOLD),
        "minified",
    )
    .await;

    let last = result.provider.requests().pop().unwrap();
    let mut joined = String::new();
    for (n, (_, is_error, page)) in results(&last).into_iter().skip(1).enumerate() {
        assert!(!is_error);
        assert!(
            page.len() < PAGE + 200,
            "page {n} holds {} bytes",
            page.len()
        );
        match page.rsplit_once("\n[line 1 continues at byte ") {
            Some((text, hint)) => {
                assert!(hint.starts_with(&format!("{};", (n + 1) * PAGE)), "{hint}");
                joined.push_str(text);
            }
            None => joined.push_str(&page),
        }
    }
    assert_eq!(pages, 31);
    assert_eq!(joined, line, "the pages are the exact bytes");
}

/// A result under the threshold reaches the model exactly as it does with
/// offload off, for each kind.
#[tokio::test]
async fn results_under_the_threshold_are_unchanged() {
    let small = rows(5);
    let cases: Vec<ToolResult> = vec![
        Ok(ToolOutput::new("x".repeat(3_000))),
        Ok(ToolOutput::new(small)),
        Err(ToolOutputError::new("e".repeat(3_000))),
    ];
    for result in cases {
        let on = run(result.clone(), &[], Some(THRESHOLD), "t").await;
        let off = run(result, &[], None, "t").await;
        assert_eq!(seen(&on, "call_1"), seen(&off, "call_1"));
        assert_eq!(
            tool_end(&on, "call_1").metadata,
            tool_end(&off, "call_1").metadata
        );
        assert_eq!(
            tool_end(&on, "call_1").error_code,
            tool_end(&off, "call_1").error_code
        );
        let backend = on.sessions.backend(&SessionId::from("s1"));
        assert!(backend.ls("").await.unwrap().is_empty());
    }
}

/// The response for a request with `n` assistant messages is `turns[n]`,
/// so a turn asked again after a crash gets the same answer.
struct Replay(Vec<ScriptedTurn>);

#[async_trait]
impl Provider for Replay {
    fn id(&self) -> &str {
        "replay"
    }
    async fn stream(
        &self,
        request: ProviderRequest,
        _cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let turn = (request.messages.iter())
            .filter(|message| message.role == Role::Assistant)
            .count();
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            self.0[turn].events.iter().cloned().map(Ok).collect();
        let stream: BoxStream<'static, _> = futures::stream::iter(events).boxed();
        Ok(stream)
    }
}

/// The tool-result texts of the stored session, by call id.
async fn logged_results(store: &MemorySessionStore) -> HashMap<String, String> {
    let entries = store.load(NS, &SessionId::from("s1")).await.unwrap();
    let mut found = HashMap::new();
    for entry in entries {
        let linch_core::LogEntry::Message(entry) = entry else {
            continue;
        };
        for block in &entry.message.content {
            if let ContentBlock::ToolResult(result) = block {
                let text = (result.content.iter())
                    .map(|part| format!("{part:?}"))
                    .collect();
                found.insert(result.tool_use_id.clone(), text);
            }
        }
    }
    found
}

/// The agent, its stores and the offload backend for one crash trial.
struct Trial {
    sessions: Arc<MemorySessions>,
    runs: Arc<MemoryRunStore>,
    store: Arc<MemorySessionStore>,
    tool_runs: Arc<Mutex<Vec<Arc<AtomicUsize>>>>,
}

impl Trial {
    fn new() -> Self {
        Self {
            sessions: Arc::new(MemorySessions::new()),
            runs: Arc::new(MemoryRunStore::new()),
            store: Arc::new(MemorySessionStore::new()),
            tool_runs: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn agent(&self, run_store: Arc<dyn RunStore>, session_store: Arc<dyn SessionStore>) -> Agent {
        let files: Arc<dyn SessionFiles> = self.sessions.clone();
        let (tool, runs) = Fixed::new("rows", Ok(ToolOutput::new(rows(6_500))));
        self.tool_runs.lock().unwrap().push(runs);
        let provider = Replay(turns(
            "rows",
            &[json!({"path": rows_path(), "offset": 1, "limit": 2})],
        ));
        AgentBuilder::new(AgentSpec::new(MODEL))
            .provider(Arc::new(provider))
            .tools(registry(files.clone(), tool))
            .hooks(hooks(files, Some(THRESHOLD)))
            .permissions(permissions())
            .run_store(run_store)
            .session_store(session_store)
            .namespace(NS)
            .build()
            .unwrap()
    }

    fn tool_runs(&self) -> usize {
        let runs = self.tool_runs.lock().unwrap();
        runs.iter().map(|runs| runs.load(Ordering::SeqCst)).sum()
    }
}

async fn recover(agent: &Agent, runs: &MemoryRunStore, run_id: &RunId) {
    match runs.load(NS, run_id).await {
        Ok(record) if record.status.is_terminal() => {}
        Ok(_) => {
            let mut stream = agent.resume(run_id.clone(), RunOptions::new("s1"));
            while let Some(item) = stream.next().await {
                item.expect("the resumed run succeeds");
            }
        }
        Err(StoreError::NotFound { .. }) => {
            let mut stream = agent.run("go", RunOptions::new("s1"));
            while let Some(item) = stream.next().await {
                item.expect("the fresh run succeeds");
            }
        }
        Err(error) => panic!("load failed: {error}"),
    }
}

/// Crash a run on its `offload` call at every run-store write and every
/// session write, then continue it: the pointer is the call's result every
/// time, or the call is reported interrupted, and the payload is never
/// logged.
#[tokio::test]
async fn a_crash_at_any_write_keeps_the_pointer_the_result() {
    let clean = Trial::new();
    let agent = clean.agent(clean.runs.clone(), clean.store.clone());
    let mut stream = agent.run("go", RunOptions::new("s1"));
    while let Some(item) = stream.next().await {
        item.unwrap();
    }
    let want = logged_results(&clean.store).await;
    let pointer = want["call_1"].clone();
    assert!(pointer.contains(&format!("offloaded to '{}'", rows_path())));
    let pretty = serde_json::to_string_pretty(&rows(6_500)).unwrap();

    let mut crashes = 0;
    let mut outcomes: HashMap<bool, u32> = HashMap::new();
    for target in ["runs", "sessions"] {
        for n in 1..=40u64 {
            for timing in [FaultTiming::Before, FaultTiming::After] {
                let context = format!("{target} write {n} {timing:?}");
                let trial = Trial::new();
                let (faulty_runs, faulty_sessions);
                let (run_store, session_store): (Arc<dyn RunStore>, Arc<dyn SessionStore>) =
                    match target {
                        "runs" => {
                            faulty_runs = Arc::new(Faulty::new(trial.runs.clone(), n, timing));
                            (faulty_runs, trial.store.clone())
                        }
                        _ => {
                            faulty_sessions = Arc::new(Faulty::new(trial.store.clone(), n, timing));
                            (trial.runs.clone(), faulty_sessions)
                        }
                    };
                let crashing = trial.agent(run_store, session_store);
                let mut stream = crashing.run("go", RunOptions::new("s1"));
                let run_id = stream.run_id().clone();
                let mut failed = false;
                while let Some(item) = stream.next().await {
                    failed |= item.is_err();
                }
                if !failed {
                    break;
                }
                crashes += 1;
                let again = trial.agent(trial.runs.clone(), trial.store.clone());
                recover(&again, &trial.runs, &run_id).await;
                let got = logged_results(&trial.store).await;
                let result = &got["call_1"];
                let interrupted = result.contains("was interrupted");
                assert!(*result == pointer || interrupted, "{context}: {result}");
                assert!(
                    !result.contains("row 6400"),
                    "{context}: the payload was logged"
                );
                assert!(trial.tool_runs() <= 1, "{context}: the tool ran again");
                *outcomes.entry(interrupted).or_insert(0) += 1;
                if !interrupted {
                    let backend = trial.sessions.backend(&SessionId::from("s1"));
                    let stored = backend.read(&rows_path()).await;
                    assert_eq!(
                        stored.unwrap(),
                        pretty,
                        "{context}: the pointer names a file"
                    );
                }
            }
        }
    }
    assert!(crashes > 10, "the sweep crashed {crashes} runs");
    let seen = |interrupted| outcomes.get(&interrupted).copied().unwrap_or(0);
    assert!(
        seen(false) > 0 && seen(true) > 0,
        "both outcomes occur: {outcomes:?}"
    );
}
