//! Model calls and compaction: a `chat` span per call, a `compaction` span
//! around a summary call, and the two model-call metrics.

use linch_core::Usage;
use linch_core::events::kinds::{AssistantEvent, CompactionEvent, ContextBuildEvent, UsageEvent};
use opentelemetry::trace::SpanKind;
use opentelemetry::{Array, Context, KeyValue, Value};

use super::RunObserver;
use crate::keys;
use crate::span::{Open, json, wire_name};

/// An open model call.
pub(super) struct Chat {
    open: Open,
    /// The model the call requested; the run's when unknown.
    model: Option<String>,
    usage: Option<Usage>,
    cost: Option<f64>,
    /// The model the provider reported serving the call.
    served: Option<String>,
    /// When the response arrived; its `usage` event, if any, comes next.
    answered: Option<u64>,
}

/// A summary call seen by its usage, and when.
pub(super) struct Summary {
    usage: UsageEvent,
    at: u64,
}

impl RunObserver {
    pub(super) fn context_build(&mut self, build: &ContextBuildEvent, at: u64) {
        self.flush_summary(None);
        if let Some(chat) = self.chat.take() {
            self.finish_chat(chat, at, None);
        }
        let (step, _) = self.step(at);
        let model = Some(build.model.clone());
        self.chat = Some(self.open_chat(&step, at, model));
    }

    /// A chat on `model`, the model its call requested; the run's when
    /// `None` (a summary call is on the run's).
    pub(super) fn open_chat(&self, parent: &Context, at: u64, model: Option<String>) -> Chat {
        let model = model.or_else(|| self.model.clone());
        let name = match &model {
            Some(model) => format!("{} {model}", keys::CHAT),
            None => keys::CHAT.to_owned(),
        };
        let mut attributes = self.attributes(keys::CHAT, true);
        if let Some(model) = &model {
            attributes.retain(|kv| kv.key.as_str() != keys::MODEL);
            attributes.push(KeyValue::new(keys::MODEL, model.clone()));
        }
        Chat {
            open: self.start(parent, name, SpanKind::Client, at, attributes),
            model,
            usage: None,
            cost: None,
            served: None,
            answered: None,
        }
    }

    pub(super) fn assistant(&mut self, response: &AssistantEvent, at: u64) {
        let Some(chat) = &mut self.chat else { return };
        let reason = wire_name(&response.stop_reason);
        let reasons = Value::Array(Array::String(vec![reason.into()]));
        chat.open.set(KeyValue::new(keys::FINISH_REASONS, reasons));
        if self.capture && chat.open.recording() {
            let output = json(&response.message);
            chat.open.set(KeyValue::new(keys::OUTPUT_MESSAGES, output));
        }
        chat.answered = Some(at);
    }

    pub(super) fn usage(&mut self, event: &UsageEvent, at: u64) {
        if let Some(chat) = &mut self.chat {
            chat.usage = Some(event.usage);
            chat.cost = event.cost_usd;
            chat.served.clone_from(&event.actual_model);
            self.close_answered_chat();
            return;
        }
        // No primary call is open, so this is the turn's summary call.
        let usage = event.clone();
        self.summary = Some(Summary { usage, at });
    }

    /// A compaction span from `start` holding the summary call, which
    /// ended at `end`, with the `usage` it reported.
    fn summary_span(&mut self, start: u64, end: u64, usage: Option<&UsageEvent>) -> Open {
        let (step, step_start) = self.step(start);
        let start = start.max(step_start);
        let attributes = self.attributes(keys::COMPACTION, true);
        let compaction = self.start(
            &step,
            keys::COMPACTION,
            SpanKind::Internal,
            start,
            attributes,
        );
        let mut chat = self.open_chat(compaction.context(), start, None);
        if let Some(event) = usage {
            chat.usage = Some(event.usage);
            chat.cost = event.cost_usd;
            chat.served.clone_from(&event.actual_model);
        }
        self.finish_chat(chat, end, None);
        compaction
    }

    /// A summary call that gave no compaction: a compaction span from the
    /// turn's start to where the call was seen, marked with `error` if set.
    pub(super) fn flush_summary(&mut self, error: Option<&str>) {
        let Some(Summary { usage, at }) = self.summary.take() else {
            return;
        };
        let start = self.step(at).1;
        let compaction = self.summary_span(start, at, Some(&usage));
        if let Some(error) = error {
            compaction.set_error(error);
        }
        compaction.end(at);
    }

    /// The compaction span: from `duration_ms` before its event to the
    /// event, around its summary call.
    pub(super) fn compaction(&mut self, event: &CompactionEvent, at: u64) {
        let summary = self.summary.take();
        // A summary call that reported no usage ends with the compaction.
        let end = summary.as_ref().map_or(at, |summary| summary.at);
        let start = at.saturating_sub(event.duration_ms);
        let usage = summary.as_ref().map(|summary| &summary.usage);
        let compaction = self.summary_span(start, end, usage);
        let strategy = KeyValue::new(keys::COMPACTION_STRATEGY, event.strategy.clone());
        compaction.set(strategy);
        compaction.end(at);
    }

    /// Close a chat whose response arrived and whose usage did not follow.
    pub(super) fn close_answered_chat(&mut self) {
        if let Some(at) = self.chat.as_ref().and_then(|chat| chat.answered)
            && let Some(chat) = self.chat.take()
        {
            self.finish_chat(chat, at, None);
        }
    }

    /// End the chat: its span, its duration and its token counts.
    pub(super) fn finish_chat(&self, chat: Chat, at: u64, error: Option<&str>) {
        let Chat {
            open,
            model,
            usage,
            cost,
            served,
            ..
        } = chat;
        let mut attributes = vec![KeyValue::new(keys::OPERATION, keys::CHAT)];
        for (key, value) in [(keys::PROVIDER, &self.provider), (keys::MODEL, &model)] {
            if let Some(value) = value {
                attributes.push(KeyValue::new(key, value.clone()));
            }
        }
        if let Some(error) = error {
            open.set_error(error);
            attributes.push(KeyValue::new(keys::ERROR_TYPE, error.to_owned()));
        }
        if let Some(usage) = &usage {
            open.set_usage(usage);
        }
        if let Some(cost) = cost {
            open.set(KeyValue::new(keys::COST_USD, cost));
        }
        if let Some(served) = served {
            open.set(KeyValue::new(keys::RESPONSE_MODEL, served));
        }
        let seconds = at.saturating_sub(open.start_ms()) as f64 / 1000.0;
        open.end(at);
        self.telemetry.record_duration(seconds, &attributes);
        let tokens = usage.map(|u| {
            [
                ("input", u.total_input_tokens()),
                ("output", u.output_tokens),
            ]
        });
        for (kind, tokens) in tokens.into_iter().flatten() {
            let mut attributes = attributes.clone();
            attributes.push(KeyValue::new(keys::TOKEN_TYPE, kind));
            self.telemetry.record_tokens(tokens, &attributes);
        }
    }
}
