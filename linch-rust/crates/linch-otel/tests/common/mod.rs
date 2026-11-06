//! Shared fixtures: an SDK with in-memory exporters, scripted agents on
//! memory stores, and lookups over the finished spans and metrics.

#![allow(dead_code, unused_imports)]

mod wire;

pub use wire::*;

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use futures::StreamExt;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, Envelope, JsonObject, MemoryRunStore, MemorySessionStore,
    PermissionEngine, PermissionMode, PermissionPolicy, RunStream, ScriptedProvider, ScriptedTurn,
    Tool, ToolContext, ToolOutput, ToolRegistry, ToolResult, ToolScope, Usage,
};
use linch_otel::{RunObserver, Telemetry};
use opentelemetry::metrics::MeterProvider as _;
use opentelemetry::trace::{SpanId, Status, Tracer as _, TracerProvider as _};
use opentelemetry::{KeyValue, Value};
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, HistogramDataPoint, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider, SpanData};
use serde_json::json;

pub const MODEL: &str = "scripted-model";

/// An SDK whose spans and metrics stay in memory.
pub struct Sdk {
    spans: InMemorySpanExporter,
    metrics: InMemoryMetricExporter,
    tracer_provider: SdkTracerProvider,
    meter_provider: SdkMeterProvider,
    pub telemetry: Telemetry,
}

impl Sdk {
    pub fn new() -> Self {
        let spans = InMemorySpanExporter::default();
        let metrics = InMemoryMetricExporter::default();
        let tracer_provider = SdkTracerProvider::builder()
            .with_simple_exporter(spans.clone())
            .build();
        let meter_provider = SdkMeterProvider::builder()
            .with_reader(PeriodicReader::builder(metrics.clone()).build())
            .build();
        let telemetry = Telemetry::new(
            tracer_provider.tracer(linch_otel::SCOPE),
            &meter_provider.meter(linch_otel::SCOPE),
        );
        Self {
            spans,
            metrics,
            tracer_provider,
            meter_provider,
            telemetry,
        }
    }

    /// An observer with the scripted provider's id and capture off.
    pub fn observer(&self) -> RunObserver {
        self.telemetry
            .observer()
            .provider("scripted")
            .capture_content(false)
    }

    /// A host span to parent a run under.
    pub fn host_span(&self) -> opentelemetry_sdk::trace::Span {
        self.tracer_provider.tracer("host").start("host request")
    }

    /// Every finished span, in end order.
    pub fn spans(&self) -> Vec<SpanData> {
        self.spans.get_finished_spans().unwrap()
    }

    /// The data points of histogram `name`, `u64` or `f64` as `f64`, as of
    /// now.
    pub fn points(&self, name: &str) -> Vec<Point> {
        self.meter_provider.force_flush().unwrap();
        let mut points = Vec::new();
        // Each flush exports every cumulative point again: read the last.
        let exported = self.metrics.get_finished_metrics().unwrap();
        let Some(resource) = exported.last() else {
            return points;
        };
        let metrics = resource.scope_metrics().flat_map(|scope| scope.metrics());
        for metric in metrics.filter(|m| m.name() == name) {
            match metric.data() {
                AggregatedMetrics::U64(MetricData::Histogram(h)) => {
                    points.extend(h.data_points().map(|p| Point::of(p, |v| v as f64)));
                }
                AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                    points.extend(h.data_points().map(|p| Point::of(p, |v| v)));
                }
                other => panic!("{name} is not a histogram: {other:?}"),
            }
        }
        points
    }

    /// The unit metric `name` was created with.
    pub fn unit(&self, name: &str) -> String {
        self.meter_provider.force_flush().unwrap();
        let exported = self.metrics.get_finished_metrics().unwrap();
        let metric = exported
            .iter()
            .flat_map(|r| r.scope_metrics())
            .flat_map(|s| s.metrics())
            .find(|m| m.name() == name)
            .unwrap_or_else(|| panic!("no metric {name}"));
        metric.unit().to_owned()
    }
}

/// One histogram data point: its attributes, count and sum.
#[derive(Debug, Clone)]
pub struct Point {
    pub attributes: Vec<KeyValue>,
    pub count: u64,
    pub sum: f64,
}

impl Point {
    fn of<T: Copy>(point: &HistogramDataPoint<T>, as_f64: impl Fn(T) -> f64) -> Self {
        Self {
            attributes: point.attributes().cloned().collect(),
            count: point.count(),
            sum: as_f64(point.sum()),
        }
    }

    pub fn attr(&self, key: &str) -> Option<String> {
        let found = self.attributes.iter().find(|kv| kv.key.as_str() == key);
        found.map(|kv| kv.value.to_string())
    }
}

/// The one span named `name`.
pub fn span<'a>(spans: &'a [SpanData], name: &str) -> &'a SpanData {
    let found: Vec<_> = spans.iter().filter(|s| s.name == name).collect();
    assert_eq!(found.len(), 1, "one span named {name}: {:?}", names(spans));
    found[0]
}

/// Every span named `name`, in start order.
pub fn all<'a>(spans: &'a [SpanData], name: &str) -> Vec<&'a SpanData> {
    let mut found: Vec<_> = spans.iter().filter(|s| s.name == name).collect();
    found.sort_by_key(|s| s.start_time);
    found
}

pub fn names(spans: &[SpanData]) -> Vec<String> {
    let mut names: Vec<_> = spans.iter().map(|s| s.name.to_string()).collect();
    names.sort();
    names
}

pub fn attr<'a>(span: &'a SpanData, key: &str) -> Option<&'a Value> {
    let found = span.attributes.iter().find(|kv| kv.key.as_str() == key);
    found.map(|kv| &kv.value)
}

pub fn text(span: &SpanData, key: &str) -> Option<String> {
    attr(span, key).map(|v| v.to_string())
}

pub fn int(span: &SpanData, key: &str) -> Option<i64> {
    match attr(span, key)? {
        Value::I64(v) => Some(*v),
        other => panic!("{key} is not an integer: {other:?}"),
    }
}

pub fn error_type(span: &SpanData) -> Option<String> {
    match &span.status {
        Status::Error { .. } => Some(text(span, "error.type").expect("an error has its type")),
        _ => {
            assert!(attr(span, "error.type").is_none(), "{}", span.name);
            None
        }
    }
}

/// Whether `child`'s parent is `parent`, in the same trace.
pub fn is_child(child: &SpanData, parent: &SpanData) -> bool {
    child.parent_span_id == parent.span_context.span_id()
        && child.span_context.trace_id() == parent.span_context.trace_id()
}

pub fn is_root(span: &SpanData) -> bool {
    span.parent_span_id == SpanId::INVALID
}

/// Unix milliseconds as the SDK's span time.
pub fn at(ms: u64) -> SystemTime {
    UNIX_EPOCH + Duration::from_millis(ms)
}

/// The `ts_ms` of the first envelope of `kind`, from index `from`.
pub fn ts(envelopes: &[Envelope], kind: &str, from: usize) -> u64 {
    let found = envelopes[from..]
        .iter()
        .find(|e| e.event.type_name() == kind);
    found.unwrap_or_else(|| panic!("no {kind}")).ts_ms
}

/// The `ts_ms` of every envelope of `kind`.
pub fn all_ts(envelopes: &[Envelope], kind: &str) -> Vec<u64> {
    let found = envelopes.iter().filter(|e| e.event.type_name() == kind);
    found.map(|e| e.ts_ms).collect()
}

/// Drain `stream` into `observer`; the envelopes, in order.
pub async fn observe(stream: RunStream, observer: &mut RunObserver) -> Vec<Envelope> {
    let mut stream = stream;
    let mut envelopes = Vec::new();
    while let Some(item) = stream.next().await {
        let envelope = item.expect("the run does not fail");
        observer.observe(&envelope);
        envelopes.push(envelope);
    }
    envelopes
}

/// An `Envelope` from its JSON form.
pub fn envelope(seq: u64, ts_ms: u64, event: serde_json::Value) -> Envelope {
    let value = json!({"v": 1, "seq": seq, "run_id": "r1", "ts_ms": ts_ms, "event": event});
    Envelope::from_value(value).unwrap()
}

/// A tool that returns its `value` input.
pub struct Echo {
    name: &'static str,
    scope: ToolScope,
    schema: JsonObject,
}

impl Echo {
    pub fn new(name: &'static str, scope: ToolScope) -> Self {
        let schema = json!({"type": "object", "properties": {"value": {"type": "string"}}});
        let schema = schema.as_object().unwrap().clone();
        Self {
            name,
            scope,
            schema,
        }
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
    async fn execute(&self, input: JsonObject, _ctx: ToolContext) -> ToolResult {
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

pub fn text_turn(text: &str) -> ScriptedTurn {
    let builder = TurnBuilder::new(MODEL).text(text);
    builder.usage(usage(10, 5)).build()
}

/// A response with `k` calls of `tool`, ids `call_{turn}_{i}`.
pub fn tool_turn(turn: usize, k: usize, tool: &str) -> ScriptedTurn {
    let mut builder = TurnBuilder::new(MODEL).text("working");
    for i in 0..k {
        let value = json!({"value": format!("v{turn}.{i}")});
        builder = builder.tool_call(&format!("call_{turn}_{i}"), tool, &value);
    }
    builder.usage(usage(20, 7)).build()
}

/// An agent on memory stores with the given spec, turns and tools.
pub fn agent_with(
    spec: AgentSpec,
    turns: Vec<ScriptedTurn>,
    tools: Vec<Echo>,
    mode: PermissionMode,
    stores: &Stores,
) -> Agent {
    let registry = ToolRegistry::new();
    for tool in tools {
        let _ = registry.register(Arc::new(tool)).unwrap();
    }
    let permissions = PermissionEngine::new(PermissionPolicy::new(mode, "/")).unwrap();
    AgentBuilder::new(spec)
        .provider(Arc::new(ScriptedProvider::new(turns)))
        .tools(Arc::new(registry))
        .permissions(permissions)
        .run_store(stores.runs.clone())
        .session_store(stores.sessions.clone())
        .namespace("otel")
        .build()
        .unwrap()
}

pub fn agent(turns: Vec<ScriptedTurn>, tools: Vec<Echo>) -> Agent {
    agent_with(
        AgentSpec::new(MODEL),
        turns,
        tools,
        PermissionMode::Default,
        &Stores::default(),
    )
}

/// Run and session stores one agent (or a resume of its run) shares.
#[derive(Clone, Default)]
pub struct Stores {
    pub runs: Arc<MemoryRunStore>,
    pub sessions: Arc<MemorySessionStore>,
}
