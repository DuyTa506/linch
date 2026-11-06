//! OpenTelemetry traces and metrics for Linch runs, built from the run's
//! event stream.
//!
//! Python Linch emits its spans from inside the loop. Here a [`RunObserver`]
//! reads a run's [`Envelope`](linch_core::Envelope)s as they stream and
//! builds the same spans after the fact, each started at its opening
//! event's `ts_ms` and ended at its closing event's:
//!
//! ```text
//! invoke_agent                      init .. result (or suspended)
//! └─ step                           one per turn, linch.turn_index
//!    ├─ compaction                  .. compaction
//!    │  └─ chat {model}             the summary call, .. its usage
//!    ├─ chat {model}                context_build .. assistant / error
//!    └─ execute_tool {tool}         tool_call_start .. tool_call_end
//! ```
//!
//! It records the metrics `gen_ai.client.token.usage` (unit `{token}`) and
//! `gen_ai.client.operation.duration` (unit `s`) per model call, with the
//! SDK's default buckets, as Python does. Span names, attribute keys and
//! metric names are Python's; see [`keys`].
//!
//! Only the API crate is used: the host owns the SDK and its exporters, and
//! passes its tracer and meter in ([`Telemetry::new`]), or asks for the
//! global ones explicitly ([`Telemetry::global`]). Nothing here installs or
//! reads a global provider otherwise.
//!
//! Content (the prompt, answers, tool input and results) is left out unless
//! capture is on: [`RunObserver::capture_content`], which defaults to
//! Python's switch, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`
//! ([`capture_content_from_env`]).
//!
//! # Example
//!
//! A scripted run traced into the SDK's in-memory exporter:
//!
//! ```
//! use std::sync::Arc;
//!
//! use futures::StreamExt;
//! use linch_core::provider::scripted::TurnBuilder;
//! use linch_core::{
//!     AgentBuilder, AgentSpec, MemoryRunStore, MemorySessionStore, PermissionEngine,
//!     PermissionMode, PermissionPolicy, RunOptions, ScriptedProvider,
//! };
//! use linch_otel::Telemetry;
//! use opentelemetry::metrics::MeterProvider as _;
//! use opentelemetry::trace::TracerProvider as _;
//! use opentelemetry_sdk::metrics::SdkMeterProvider;
//! use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};
//!
//! # tokio::runtime::Builder::new_current_thread().build().unwrap().block_on(async {
//! let spans = InMemorySpanExporter::default();
//! let tracer_provider = SdkTracerProvider::builder()
//!     .with_simple_exporter(spans.clone())
//!     .build();
//! let meter_provider = SdkMeterProvider::builder().build();
//! let telemetry = Telemetry::new(
//!     tracer_provider.tracer(linch_otel::SCOPE),
//!     &meter_provider.meter(linch_otel::SCOPE),
//! );
//!
//! let provider = ScriptedProvider::new([TurnBuilder::new("m").text("hello").build()]);
//! let permissions = PermissionEngine::new(PermissionPolicy::new(PermissionMode::Default, "/"))
//!     .unwrap();
//! let agent = AgentBuilder::new(AgentSpec::new("m"))
//!     .provider(Arc::new(provider))
//!     .permissions(permissions)
//!     .run_store(Arc::new(MemoryRunStore::default()))
//!     .session_store(Arc::new(MemorySessionStore::default()))
//!     .namespace("docs")
//!     .build()
//!     .unwrap();
//!
//! let mut observer = telemetry.observer().provider("scripted");
//! let mut stream = agent.run("hi", RunOptions::new("s1"));
//! while let Some(item) = stream.next().await {
//!     match item {
//!         Ok(envelope) => observer.observe(&envelope),
//!         // A type name only: an error message may hold private content.
//!         Err(_) => observer.fail("RunError"),
//!     }
//! }
//! drop(observer);
//!
//! let mut names: Vec<_> = spans
//!     .get_finished_spans()
//!     .unwrap()
//!     .into_iter()
//!     .map(|span| span.name.into_owned())
//!     .collect();
//! names.sort();
//! assert_eq!(names, ["chat m", "invoke_agent", "step"]);
//! # });
//! ```
//!
//! # What the stream does not give
//!
//! The event stream carries no store calls, so Python's `linch.store.duration`
//! metric is not built here. It carries neither the model that served a call
//! (`gen_ai.response.model`) nor the request's messages (a chat span's
//! `gen_ai.input.messages`), nor a provider id: set [`RunObserver::provider`].
//! A resumed call has no `init` event, so its session, model and turn come
//! from the host through the observer's setters. Subagent events reach the
//! parent's stream wrapped, after the fact and without their own times, so
//! they are not traced.

pub mod keys;
mod observer;
mod span;

use std::sync::Arc;

use opentelemetry::global::{self, BoxedTracer};
use opentelemetry::metrics::{Histogram, Meter};
use opentelemetry::trace::{TraceContextExt, Tracer};
use opentelemetry::{Context, KeyValue};

pub use observer::RunObserver;

/// The instrumentation scope name Python Linch uses for its tracer and meter;
/// pass it to the host's providers when making them for [`Telemetry::new`].
pub const SCOPE: &str = "linch";

/// A tracer and the two model-call histograms, shared by every run traced
/// with them. Cheap to clone.
#[derive(Clone)]
pub struct Telemetry {
    inner: Arc<Instruments>,
}

struct Instruments {
    tracer: BoxedTracer,
    tokens: Histogram<u64>,
    duration: Histogram<f64>,
}

impl Telemetry {
    /// Trace with the host's `tracer` and record metrics with its `meter`.
    ///
    /// Args:
    ///     tracer: Any API tracer, such as an SDK tracer from the host's
    ///         provider made for [`SCOPE`].
    ///     meter: The meter the two histograms are created on, once.
    pub fn new<T>(tracer: T, meter: &Meter) -> Self
    where
        T: Tracer + Send + Sync + 'static,
        T::Span: Send + Sync + 'static,
    {
        let tokens = meter
            .u64_histogram(keys::TOKEN_USAGE)
            .with_unit("{token}")
            .build();
        let duration = meter
            .f64_histogram(keys::OPERATION_DURATION)
            .with_unit("s")
            .build();
        Self {
            inner: Arc::new(Instruments {
                tracer: BoxedTracer::new(Box::new(tracer)),
                tokens,
                duration,
            }),
        }
    }

    /// Trace and record with the process-global tracer and meter provider,
    /// under [`SCOPE`]. The host opts in to global state by calling this.
    pub fn global() -> Self {
        Self::new(global::tracer(SCOPE), &global::meter(SCOPE))
    }

    /// A fresh observer for one run (or one resume or approve of a run).
    ///
    /// Content capture starts at [`capture_content_from_env`]; the parent
    /// context starts empty, so the run span is a trace root.
    pub fn observer(&self) -> RunObserver {
        RunObserver::new(self.clone(), capture_content_from_env())
    }

    fn tracer(&self) -> &BoxedTracer {
        &self.inner.tracer
    }

    fn record_duration(&self, seconds: f64, attributes: &[KeyValue]) {
        self.inner.duration.record(seconds, attributes);
    }

    fn record_tokens(&self, tokens: u64, attributes: &[KeyValue]) {
        self.inner.tokens.record(tokens, attributes);
    }
}

impl std::fmt::Debug for Telemetry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Telemetry")
    }
}

/// Whether Python's content switch is on: the environment variable
/// [`keys::CAPTURE_CONTENT_ENV`] is `true`, in any case.
pub fn capture_content_from_env() -> bool {
    capture_switch(std::env::var(keys::CAPTURE_CONTENT_ENV).ok().as_deref())
}

fn capture_switch(value: Option<&str>) -> bool {
    value.is_some_and(|value| value.eq_ignore_ascii_case("true"))
}

/// The `gen_ai.provider.name` for a Linch provider id, as Python maps it:
/// both OpenAI APIs are `openai`, Gemini is `gcp.gemini`, any other id is
/// itself.
pub fn provider_name(id: &str) -> &str {
    match id {
        "openai-chat" | "openai-responses" => "openai",
        "gemini" => "gcp.gemini",
        other => other,
    }
}

/// The 32-character hex trace id of the span in `cx`, or `None` without a
/// valid one, like Python's `span_trace_id`.
pub fn span_trace_id(cx: &Context) -> Option<String> {
    let span = cx.span();
    let context = span.span_context();
    context.is_valid().then(|| context.trace_id().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_content_switch_is_true_in_any_case_and_off_otherwise() {
        assert!(capture_switch(Some("true")));
        assert!(capture_switch(Some("TRUE")));
        assert!(capture_switch(Some("True")));
        assert!(!capture_switch(Some("1")));
        assert!(!capture_switch(Some("yes")));
        assert!(!capture_switch(Some("")));
        assert!(!capture_switch(None));
    }

    #[test]
    fn provider_ids_map_as_in_python() {
        assert_eq!(provider_name("openai-chat"), "openai");
        assert_eq!(provider_name("openai-responses"), "openai");
        assert_eq!(provider_name("gemini"), "gcp.gemini");
        assert_eq!(provider_name("anthropic"), "anthropic");
        assert_eq!(provider_name("vllm"), "vllm");
    }

    #[test]
    fn observers_move_between_tasks() {
        fn send<T: Send>() {}
        send::<Telemetry>();
        send::<RunObserver>();
    }

    #[test]
    fn an_empty_context_has_no_trace_id() {
        assert_eq!(span_trace_id(&Context::new()), None);
    }
}
