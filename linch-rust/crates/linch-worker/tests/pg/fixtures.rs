//! The agent under test: a tool that counts its calls and can hold its
//! first one, and a request-keyed scripted provider.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use async_trait::async_trait;
use futures::StreamExt;
use futures::stream::BoxStream;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, ContentBlock, JsonObject, Message, PermissionEngine,
    PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent, ProviderRequest,
    ProviderStream, Role, ScriptedTurn, Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult,
    ToolScope,
};
use linch_postgres::{PgPool, PgRunStore, PgSessionStore};
use serde_json::json;
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;

use crate::common::NS;

pub const MODEL: &str = "scripted-model";

/// What every probe of one test saw.
#[derive(Debug, Default)]
pub struct Stats {
    /// Calls that started, per run id.
    pub runs: HashMap<String, u32>,
    in_flight: HashMap<String, u32>,
    total: u32,
    /// Most calls of one run in flight at once.
    pub max_per_run: u32,
    /// Most calls in flight at once, over all runs.
    pub max_total: u32,
}

/// How a probe's first call behaves.
#[derive(Clone)]
pub enum Hold {
    /// Returns at once.
    None,
    /// Sleeps (asynchronously) on every call.
    Sleep(Duration),
    /// The first call waits for a permit that never comes.
    Forever(Arc<Semaphore>),
    /// The first call blocks its thread until the sender sends or drops,
    /// freezing a current-thread runtime.
    Freeze(Arc<Mutex<Option<mpsc::Receiver<()>>>>),
}

/// A tool that counts its calls per run and can hold its first call.
#[derive(Clone)]
pub struct Probe {
    name: &'static str,
    scope: ToolScope,
    schema: JsonObject,
    hold: Hold,
    pub stats: Arc<Mutex<Stats>>,
}

impl Probe {
    pub fn new(name: &'static str, scope: ToolScope, hold: Hold) -> Self {
        let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
        Self {
            name,
            scope,
            schema: schema.as_object().unwrap().clone(),
            hold,
            stats: Arc::default(),
        }
    }

    /// The same tool for another worker, sharing the stats.
    pub fn with_hold(&self, hold: Hold) -> Self {
        Self {
            hold,
            ..self.clone()
        }
    }

    pub fn started(&self) -> u32 {
        self.stats.lock().unwrap().runs.values().sum()
    }

    fn enter(&self, run: &str) -> u32 {
        let mut stats = self.stats.lock().unwrap();
        *stats.runs.entry(run.to_owned()).or_default() += 1;
        let current = stats.in_flight.entry(run.to_owned()).or_default();
        *current += 1;
        let current = *current;
        stats.total += 1;
        stats.max_per_run = stats.max_per_run.max(current);
        stats.max_total = stats.max_total.max(stats.total);
        stats.runs.values().sum()
    }

    fn leave(&self, run: &str) {
        let mut stats = self.stats.lock().unwrap();
        *stats.in_flight.get_mut(run).unwrap() -= 1;
        stats.total -= 1;
    }
}

#[async_trait]
impl Tool for Probe {
    fn name(&self) -> &str {
        self.name
    }
    fn description(&self) -> &str {
        "Look a value up."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        self.scope
    }
    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let run = ctx.run_id.as_str().to_owned();
        let first = self.enter(&run) == 1;
        match &self.hold {
            Hold::None => {}
            Hold::Sleep(duration) => tokio::time::sleep(*duration).await,
            Hold::Forever(gate) if first => drop(gate.acquire().await),
            Hold::Freeze(gate) if first => {
                let receiver = gate.lock().unwrap().take();
                // Blocks the thread on purpose: the whole runtime stalls.
                if let Some(receiver) = receiver {
                    let _ = receiver.recv();
                }
            }
            Hold::Forever(_) | Hold::Freeze(_) => {}
        }
        self.leave(&run);
        Ok(ToolOutput::new(input["value"].clone()))
    }
}

/// Answers by the request, not by call order: the response to a request
/// with `n` assistant messages after its last prompt is `turns[n]`, so a
/// resumed run on another worker gets the right turn, and a later run in
/// the session starts again at the first.
pub struct Replay {
    turns: Vec<ScriptedTurn>,
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
        let is_prompt = |message: &&Message| {
            let result = |block: &ContentBlock| matches!(block, ContentBlock::ToolResult(_));
            message.role == Role::User && !message.content.iter().any(result)
        };
        let prompt = request.messages.iter().rposition(|m| is_prompt(&m));
        let turn = (request.messages[prompt.map_or(0, |i| i + 1)..].iter())
            .filter(|message| message.role == Role::Assistant)
            .count();
        let events: Vec<Result<ProviderEvent, ProviderError>> =
            self.turns[turn].events.iter().cloned().map(Ok).collect();
        let stream: BoxStream<'static, _> = futures::stream::iter(events).boxed();
        Ok(stream)
    }
}

/// Holds every model call until the test opens it.
pub struct Gate {
    /// Notified when a call reaches the gate.
    pub entered: Notify,
    /// One permit per call let through.
    pub open: Semaphore,
}

impl Default for Gate {
    fn default() -> Self {
        Self {
            entered: Notify::new(),
            open: Semaphore::new(0),
        }
    }
}

/// [`Replay`] behind a [`Gate`].
struct Gated {
    replay: Replay,
    gate: Arc<Gate>,
}

#[async_trait]
impl Provider for Gated {
    fn id(&self) -> &str {
        "gated"
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        self.gate.entered.notify_one();
        self.gate.open.acquire().await.unwrap().forget();
        self.replay.stream(request, cancel).await
    }
}

/// One call of `tool` (id `call_0`), then a text answer.
fn script(tool: &str) -> Vec<ScriptedTurn> {
    let call = TurnBuilder::new(MODEL)
        .tool_call("call_0", tool, &json!({"value": "v"}))
        .build();
    let answer = TurnBuilder::new(MODEL).text("done").build();
    vec![call, answer]
}

/// An agent on the Postgres stores of `pool` with `probe` as its one tool.
/// Default permissions ask for write and exec tools.
pub fn agent(pool: &PgPool, probe: Probe) -> Agent {
    let replay = Replay {
        turns: script(probe.name),
    };
    agent_with(pool, probe, Arc::new(replay))
}

/// [`agent`] whose model calls wait at `gate`.
pub fn gated_agent(pool: &PgPool, probe: Probe, gate: Arc<Gate>) -> Agent {
    let replay = Replay {
        turns: script(probe.name),
    };
    agent_with(pool, probe, Arc::new(Gated { replay, gate }))
}

fn agent_with(pool: &PgPool, probe: Probe, provider: Arc<dyn Provider>) -> Agent {
    let registry = ToolRegistry::new();
    // Dropping the handle keeps the registration.
    let _ = registry.register(Arc::new(probe)).unwrap();
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    AgentBuilder::new(AgentSpec::new(MODEL))
        .provider(provider)
        .tools(Arc::new(registry))
        .permissions(PermissionEngine::new(policy).unwrap())
        .run_store(Arc::new(PgRunStore::new(pool.clone())))
        .session_store(Arc::new(PgSessionStore::new(pool.clone())))
        .namespace(NS)
        .build()
        .unwrap()
}
