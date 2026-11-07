//! Shared by `tests/live_e2e.rs` and `examples/e2e_bench.rs`: a provider
//! wrapper that times each call, the weather tool, and the live agent.

#![allow(dead_code)]

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, JsonObject, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, Provider, ProviderError, ProviderEvent,
    ProviderRequest, ProviderStream, ReasoningEffort, StopReason, Thinking, Tool, ToolContext,
    ToolOutput, ToolRegistry, ToolResult, ToolScope, Usage,
};
use linch_providers::{DeepSeek, DeepSeekOptions};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// The current DeepSeek Flash id; `deepseek-v4-flash` is an alias.
pub const MODEL: &str = "deepseek-flash";

/// One provider call as the agent saw it.
#[derive(Debug, Clone, Default)]
pub struct CallRecord {
    /// Stop reason from `message_end`.
    pub stop: Option<StopReason>,
    /// Usage from `message_end`.
    pub usage: Option<Usage>,
    /// From `stream()` entry to the first event.
    pub first_event: Option<Duration>,
    /// From `stream()` entry to the last item of the stream.
    pub total: Duration,
}

/// Wraps a provider and records every call.
pub struct Timed<P> {
    inner: P,
    pub calls: Arc<Mutex<Vec<CallRecord>>>,
}

impl<P> Timed<P> {
    pub fn new(inner: P) -> Self {
        Self {
            inner,
            calls: Arc::default(),
        }
    }
}

#[async_trait]
impl<P: Provider> Provider for Timed<P> {
    fn id(&self) -> &str {
        self.inner.id()
    }

    async fn stream(
        &self,
        request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        let started = Instant::now();
        let index = {
            let mut calls = self.calls.lock().unwrap();
            calls.push(CallRecord::default());
            calls.len() - 1
        };
        let calls = Arc::clone(&self.calls);
        let stream = self.inner.stream(request, cancel).await?;
        let stream = stream.inspect(move |item| {
            let mut calls = calls.lock().unwrap();
            let record = &mut calls[index];
            record.first_event.get_or_insert_with(|| started.elapsed());
            record.total = started.elapsed();
            if let Ok(ProviderEvent::MessageEnd(end)) = item {
                record.stop = Some(end.stop_reason);
                record.usage = end.usage;
            }
        });
        Ok(stream.boxed())
    }
}

/// `get_weather(city)`: read scope, a fixed answer, counts its runs.
pub struct Weather {
    schema: JsonObject,
    pub runs: AtomicU32,
}

impl Weather {
    pub fn new() -> Self {
        let schema = json!({
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        });
        Self {
            schema: schema.as_object().cloned().unwrap_or_default(),
            runs: AtomicU32::new(0),
        }
    }
}

#[async_trait]
impl Tool for Weather {
    fn name(&self) -> &str {
        "get_weather"
    }
    fn description(&self) -> &str {
        "Get the current weather for a city."
    }
    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }
    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
        self.runs.fetch_add(1, Ordering::SeqCst);
        let city = input["city"].as_str().unwrap_or("there");
        Ok(ToolOutput::new(format!(
            "Sunny, 21 C, light wind in {city}."
        )))
    }
}

/// A live agent on DeepSeek, or `None` without `DEEPSEEK_API_KEY`.
pub struct Live {
    pub agent: Agent,
    pub calls: Arc<Mutex<Vec<CallRecord>>>,
    pub weather: Arc<Weather>,
}

pub fn live(thinking: bool) -> Option<Live> {
    let options = DeepSeekOptions::from_env()?;
    let provider = Timed::new(DeepSeek::new(options).expect("HTTP client builds"));
    let calls = Arc::clone(&provider.calls);
    let weather = Arc::new(Weather::new());
    let tools = Arc::new(ToolRegistry::new());
    let _ = tools.register(weather.clone()).expect("the tool registers");
    let mut spec = AgentSpec::new(MODEL);
    spec.system_prompt = "Use the get_weather tool for weather questions, then answer in one \
                          short sentence."
        .into();
    spec.max_turns = Some(4);
    spec.max_output_tokens = Some(1024);
    spec.thinking = Some(if thinking {
        Thinking::Enabled {
            budget_tokens: None,
            display: None,
        }
    } else {
        Thinking::Disabled {}
    });
    spec.effort = thinking.then_some(ReasoningEffort::Low);
    let policy = PermissionPolicy::new(PermissionMode::Default, "/");
    let agent = AgentBuilder::new(spec)
        .provider(Arc::new(provider))
        .tools(tools)
        .permissions(PermissionEngine::new(policy).expect("valid policy"))
        .run_store(Arc::new(MemoryRunStore::new()))
        .session_store(Arc::new(MemorySessionStore::new()))
        .namespace("live")
        .build()
        .expect("valid agent");
    Some(Live {
        agent,
        calls,
        weather,
    })
}
