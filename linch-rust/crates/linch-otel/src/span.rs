//! One open span with an explicit start time, and the small attribute
//! helpers every span kind shares.

use std::borrow::Cow;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use linch_core::Usage;
use opentelemetry::global::BoxedTracer;
use opentelemetry::trace::{SpanBuilder, SpanKind, Status, TraceContextExt, Tracer};
use opentelemetry::{Context, KeyValue};
use serde::Serialize;

use crate::keys;

/// A started span, kept as the context that holds it so children can name
/// it as their parent, and the event time it started at.
pub(crate) struct Open {
    cx: Context,
    start_ms: u64,
}

impl Open {
    /// Start `name` under `parent` at `start_ms` (Unix milliseconds).
    pub(crate) fn start(
        tracer: &BoxedTracer,
        parent: &Context,
        name: impl Into<Cow<'static, str>>,
        kind: SpanKind,
        start_ms: u64,
        attributes: Vec<KeyValue>,
    ) -> Self {
        let builder = SpanBuilder::from_name(name)
            .with_kind(kind)
            .with_start_time(time(start_ms))
            .with_attributes(attributes);
        let span = tracer.build_with_context(builder, parent);
        Self {
            cx: parent.with_span(span),
            start_ms,
        }
    }

    /// The context holding this span, the parent of its children.
    pub(crate) fn context(&self) -> &Context {
        &self.cx
    }

    pub(crate) fn start_ms(&self) -> u64 {
        self.start_ms
    }

    /// Whether the span records, so content is only serialized when kept.
    pub(crate) fn recording(&self) -> bool {
        self.cx.span().is_recording()
    }

    pub(crate) fn set(&self, attribute: KeyValue) {
        self.cx.span().set_attribute(attribute);
    }

    /// Mark the span failed with the error's name only, as Python does: a
    /// message may hold private content.
    pub(crate) fn set_error(&self, error_type: &str) {
        let span = self.cx.span();
        span.set_attribute(KeyValue::new(keys::ERROR_TYPE, error_type.to_owned()));
        span.set_status(Status::error(""));
    }

    pub(crate) fn set_usage(&self, usage: &Usage) {
        self.cx.span().set_attributes(usage_attributes(usage));
    }

    /// End at `end_ms`, never before the start: wall clocks can step back.
    pub(crate) fn end(self, end_ms: u64) {
        let end_ms = end_ms.max(self.start_ms);
        self.cx.span().end_with_timestamp(time(end_ms));
    }
}

/// Unix milliseconds as a [`SystemTime`]; a value past what the platform
/// clock holds reads as the epoch rather than panicking.
pub(crate) fn time(ms: u64) -> SystemTime {
    UNIX_EPOCH
        .checked_add(Duration::from_millis(ms))
        .unwrap_or(UNIX_EPOCH)
}

/// The wall clock now, in Unix milliseconds, for an end no event marks.
pub(crate) fn now_ms() -> u64 {
    let elapsed = SystemTime::now().duration_since(UNIX_EPOCH);
    elapsed.map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// A token count as an attribute value, saturating at `i64::MAX`.
pub(crate) fn count(value: u64) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

fn usage_attributes(usage: &Usage) -> [KeyValue; 4] {
    [
        KeyValue::new(keys::INPUT_TOKENS, count(usage.input_tokens)),
        KeyValue::new(keys::OUTPUT_TOKENS, count(usage.output_tokens)),
        KeyValue::new(keys::CACHE_READ_TOKENS, count(usage.cache_read_tokens)),
        KeyValue::new(
            keys::CACHE_CREATION_TOKENS,
            count(usage.cache_creation_tokens),
        ),
    ]
}

/// The wire string of a unit enum such as `StopReason` (`end_turn`).
pub(crate) fn wire_name(value: &impl Serialize) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(name)) => name,
        _ => String::new(),
    }
}

/// `value` as JSON text for content capture; empty if it cannot encode.
pub(crate) fn json(value: &impl Serialize) -> String {
    serde_json::to_string(value).unwrap_or_default()
}
