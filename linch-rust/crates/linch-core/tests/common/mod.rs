//! Shared fixtures for the end-to-end tests: tools that count their runs,
//! stores that number their writes, a request-keyed scripted provider, and
//! helpers that drain a run stream.

#![allow(dead_code)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::events::Envelope;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::memory::{MemoryRunStore, MemorySessionStore};
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Commit, Event, JsonObject, JsonValue, Lease, LogEntry, Message,
    NewRun, PermissionEngine, PermissionMode, PermissionPolicy, Provider, ProviderError,
    ProviderEvent, ProviderRequest, ProviderStream, Role, RunError, RunId, RunRecord, RunStore,
    RunStream, ScriptedTurn, SessionId, SessionStore, StoreError, Tool, ToolContext, ToolOutput,
    ToolRegistry, ToolResult, ToolScope, Usage,
};
use serde_json::json;
use tokio_util::sync::CancellationToken;

pub const NS: &str = "e2e";
pub const MODEL: &str = "scripted-model";

/// Runs per `tool_use_id`, shared by every tool of one test.
pub type Runs = Arc<Mutex<HashMap<String, u32>>>;

/// A tool that returns its `value` input and counts each run.
pub struct Echo {
    name: &'static str,
    scope: ToolScope,
    schema: JsonObject,
    runs: Runs,
    /// Cancelled when the tool runs, to abort the run from inside a batch.
    cancel_on_run: Option<CancellationToken>,
}

impl Echo {
    pub fn new(name: &'static str, scope: ToolScope, runs: &Runs) -> Self {
        let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
        Self {
            name,
            scope,
            schema: schema.as_object().unwrap().clone(),
            runs: Arc::clone(runs),
            cancel_on_run: None,
        }
    }

    pub fn cancelling(mut self, token: CancellationToken) -> Self {
        self.cancel_on_run = Some(token);
        self
    }
}

#[async_trait]
impl Tool for Echo {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Echo the value."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        self.scope
    }
    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        *self
            .runs
            .lock()
            .unwrap()
            .entry(ctx.tool_use_id.clone())
            .or_default() += 1;
        ctx.report_progress("echoing", None);
        if let Some(token) = &self.cancel_on_run {
            token.cancel();
        }
        Ok(ToolOutput::new(input["value"].clone()))
    }
}

pub fn usage(input: u64, output: u64) -> Usage {
    Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    }
}

/// A response with `k` calls of `tool`, ids `call_{turn}_{i}`.
pub fn tool_turn(turn: usize, k: usize, tool: &str) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL).text("working").input_chunk(4);
    for i in 0..k {
        let value = json!({"value": format!("v{turn}.{i}")});
        builder = builder.tool_call(&format!("call_{turn}_{i}"), tool, &value);
    }
    builder.usage(usage(10, 5)).build()
}

pub fn text_turn(text: &str) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .text(text)
        .usage(usage(10, 5))
        .build()
}

/// `n` tool turns of `k` calls, then a text answer.
pub fn script(n: usize, k: usize, tool: &str) -> Vec<ScriptedTurn> {
    let mut turns: Vec<_> = (0..n).map(|t| tool_turn(t, k, tool)).collect();
    turns.push(text_turn("done"));
    turns
}

pub fn policy(mode: PermissionMode) -> PermissionEngine {
    PermissionEngine::new(PermissionPolicy::new(mode, "/")).unwrap()
}

pub fn registry(tools: Vec<Echo>) -> Arc<ToolRegistry> {
    let registry = ToolRegistry::new();
    for tool in tools {
        // Dropping the handle keeps the registration.
        let _ = registry.register(Arc::new(tool)).unwrap();
    }
    Arc::new(registry)
}

pub fn spec() -> AgentSpec {
    let mut spec = AgentSpec::new(MODEL);
    spec.system_prompt = "You are a test agent.".into();
    spec
}

/// An agent on `stores` for both run and session storage.
pub fn agent_with<S: RunStore + SessionStore + 'static>(
    spec: AgentSpec,
    provider: Arc<dyn Provider>,
    tools: Arc<ToolRegistry>,
    permissions: PermissionEngine,
    stores: Arc<S>,
) -> Agent {
    AgentBuilder::new(spec)
        .provider(provider)
        .tools(tools)
        .permissions(permissions)
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap()
}

pub fn agent<S: RunStore + SessionStore + 'static>(
    provider: Arc<dyn Provider>,
    tools: Arc<ToolRegistry>,
    permissions: PermissionEngine,
    stores: Arc<S>,
) -> Agent {
    agent_with(spec(), provider, tools, permissions, stores)
}

/// Every item of a stream: the envelopes, then the error if it ended with one.
pub async fn drain(stream: RunStream) -> (Vec<Envelope>, Option<RunError>) {
    let mut envelopes = Vec::new();
    let mut stream = stream;
    while let Some(item) = stream.next().await {
        match item {
            Ok(envelope) => envelopes.push(envelope),
            Err(error) => return (envelopes, Some(error)),
        }
    }
    (envelopes, None)
}

/// Drain a run that must end without a `RunError`.
pub async fn finish(stream: RunStream) -> Vec<Envelope> {
    let (envelopes, error) = drain(stream).await;
    assert!(error.is_none(), "run failed: {error:?}");
    envelopes
}

pub fn kinds(envelopes: &[Envelope]) -> Vec<String> {
    envelopes.iter().map(|e| kind(&e.event)).collect()
}

pub fn kind(event: &Event) -> String {
    event.to_value().unwrap()["type"]
        .as_str()
        .unwrap()
        .to_owned()
}

/// The messages of a session log, in order.
pub async fn messages(sessions: &dyn SessionStore, session: &str) -> Vec<Message> {
    let entries = sessions.load(NS, &SessionId::new(session)).await.unwrap();
    entries
        .into_iter()
        .map(|entry| match entry {
            LogEntry::Message(entry) => (*entry.message).clone(),
            LogEntry::Projection(_) => panic!("no projections in P4"),
        })
        .collect()
}

/// A provider that answers by the request, not by call order: the response
/// for a request with `n` assistant messages is `turns[n]`. A redispatched
/// turn gets the same answer again. Counts calls per turn.
pub struct Replay {
    turns: Vec<ScriptedTurn>,
    calls: Mutex<HashMap<usize, u32>>,
}

impl Replay {
    pub fn new(turns: Vec<ScriptedTurn>) -> Self {
        Self {
            turns,
            calls: Mutex::new(HashMap::new()),
        }
    }

    pub fn calls(&self) -> HashMap<usize, u32> {
        self.calls.lock().unwrap().clone()
    }
}

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
        *self.calls.lock().unwrap().entry(turn).or_default() += 1;
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            self.turns[turn].events.iter().cloned().map(Ok).collect();
        let stream: BoxStream<'static, _> = futures::stream::iter(events).boxed();
        Ok(stream)
    }
}

/// What one store write carried, for naming crash points.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Write {
    /// A run create, claim, or a session create.
    Setup,
    /// A run commit, with the kinds of its events.
    Commit(Vec<String>),
    /// A session append.
    Append,
}

impl Write {
    /// Whether the write carries a tool start or end.
    pub fn is_tool_barrier(&self) -> bool {
        matches!(self, Write::Commit(kinds)
            if kinds.iter().any(|k| k == "tool_call_start" || k == "tool_call_end"))
    }
}

/// Both memory stores behind one value, so `Faulty` numbers writes across
/// them; every landed write is logged.
#[derive(Default)]
pub struct Stores {
    pub runs: MemoryRunStore,
    pub sessions: MemorySessionStore,
    pub log: Mutex<Vec<Write>>,
}

impl Stores {
    fn landed<T>(&self, result: Result<T, StoreError>, write: Write) -> Result<T, StoreError> {
        if result.is_ok() {
            self.log.lock().unwrap().push(write);
        }
        result
    }
}

#[async_trait]
impl RunStore for Stores {
    async fn create(&self, run: NewRun) -> Result<RunRecord, StoreError> {
        self.landed(self.runs.create(run).await, Write::Setup)
    }
    async fn claim(&self, ns: &str, run_id: &RunId, owner: &str) -> Result<Lease, StoreError> {
        self.landed(self.runs.claim(ns, run_id, owner).await, Write::Setup)
    }
    async fn load(&self, ns: &str, run_id: &RunId) -> Result<RunRecord, StoreError> {
        self.runs.load(ns, run_id).await
    }
    async fn commit(&self, lease: &Lease, commit: Commit) -> Result<(), StoreError> {
        let write = Write::Commit(commit.events.iter().map(|e| kind(&e.event)).collect());
        self.landed(self.runs.commit(lease, commit).await, write)
    }
    async fn events(
        &self,
        ns: &str,
        run_id: &RunId,
        after: u64,
    ) -> Result<Vec<Envelope>, StoreError> {
        self.runs.events(ns, run_id, after).await
    }
}

#[async_trait]
impl SessionStore for Stores {
    async fn create(&self, ns: &str, session: &SessionId) -> Result<(), StoreError> {
        let result = SessionStore::create(&self.sessions, ns, session).await;
        self.landed(result, Write::Setup)
    }
    async fn load(&self, ns: &str, session: &SessionId) -> Result<Vec<LogEntry>, StoreError> {
        SessionStore::load(&self.sessions, ns, session).await
    }
    async fn append(
        &self,
        ns: &str,
        session: &SessionId,
        expected: u64,
        entries: Vec<LogEntry>,
    ) -> Result<u64, StoreError> {
        let result = self.sessions.append(ns, session, expected, entries).await;
        self.landed(result, Write::Append)
    }
}

/// The text of the first content block of a tool result in `message`.
pub fn result_texts(message: &Message) -> Vec<String> {
    let value = serde_json::to_value(message).unwrap();
    let content = value["content"].as_array().unwrap();
    content
        .iter()
        .filter(|block| block["type"] == "tool_result")
        .map(|block| {
            block["content"][0]["text"]
                .as_str()
                .unwrap_or("")
                .to_owned()
        })
        .collect()
}

pub fn object(value: JsonValue) -> JsonObject {
    value.as_object().unwrap().clone()
}
