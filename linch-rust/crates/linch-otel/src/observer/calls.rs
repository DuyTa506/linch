//! Model calls and compaction: a `chat` span per call, a `compaction` span
//! around a summary call, and the two model-call metrics.

use linch_core::Usage;
use linch_core::events::kinds::{AssistantEvent, CompactionEvent, UsageEvent};
use opentelemetry::trace::SpanKind;
use opentelemetry::{Array, Context, KeyValue, Value};

use super::RunObserver;
use crate::keys;
use crate::span::{Open, json, wire_name};

/// An open model call.
pub(super) struct Chat {
    open: Open,
    usage: Option<Usage>,
    cost: Option<f64>,
    /// When the response arrived; its `usage` event, if any, comes next.
    answered: Option<u64>,
}

/// An open compaction, and the last time its summary call was seen.
pub(super) struct Compaction {
    pub(super) open: Open,
    pub(super) last: u64,
}

impl RunObserver {
    pub(super) fn context_build(&mut self, at: u64) {
        // A summary call that gave no compaction ends where it was seen.
        if let Some(compaction) = self.compaction.take() {
            compaction.open.end(compaction.last);
        }
        if let Some(chat) = self.chat.take() {
            self.finish_chat(chat, at, None);
        }
        let (step, _) = self.step(at);
        self.chat = Some(self.open_chat(&step, at));
    }

    pub(super) fn open_chat(&self, parent: &Context, at: u64) -> Chat {
        let name = match &self.model {
            Some(model) => format!("{} {model}", keys::CHAT),
            None => keys::CHAT.to_owned(),
        };
        let attributes = self.attributes(keys::CHAT, true);
        Chat {
            open: self.start(parent, name, SpanKind::Client, at, attributes),
            usage: None,
            cost: None,
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
            self.close_answered_chat();
            return;
        }
        // No primary call is open, so this is the turn's summary call.
        let (parent, start) = self.open_compaction(at);
        self.summary_call(&parent, start, at, Some(event));
        if let Some(compaction) = &mut self.compaction {
            compaction.last = at;
        }
    }

    pub(super) fn open_compaction(&mut self, at: u64) -> (Context, u64) {
        if let Some(compaction) = &self.compaction {
            return (
                compaction.open.context().clone(),
                compaction.open.start_ms(),
            );
        }
        let (step, start) = self.step(at);
        let attributes = self.attributes(keys::COMPACTION, true);
        let open = self.start(
            &step,
            keys::COMPACTION,
            SpanKind::Internal,
            start,
            attributes,
        );
        let opened = (open.context().clone(), start);
        self.compaction = Some(Compaction { open, last: at });
        opened
    }

    /// The compaction's summary call, from the turn's start to `end`.
    pub(super) fn summary_call(
        &self,
        parent: &Context,
        start: u64,
        end: u64,
        usage: Option<&UsageEvent>,
    ) {
        let mut chat = self.open_chat(parent, start);
        if let Some(event) = usage {
            chat.usage = Some(event.usage);
            chat.cost = event.cost_usd;
        }
        self.finish_chat(chat, end, None);
    }

    pub(super) fn compaction(&mut self, event: &CompactionEvent, at: u64) {
        if self.compaction.is_none() {
            // Its summary call reported no usage.
            let (parent, start) = self.open_compaction(at);
            self.summary_call(&parent, start, at, None);
        }
        if let Some(compaction) = self.compaction.take() {
            let strategy = KeyValue::new(keys::COMPACTION_STRATEGY, event.strategy.clone());
            compaction.open.set(strategy);
            compaction.open.end(at);
        }
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
            open, usage, cost, ..
        } = chat;
        let mut attributes = vec![KeyValue::new(keys::OPERATION, keys::CHAT)];
        for (key, value) in [(keys::PROVIDER, &self.provider), (keys::MODEL, &self.model)] {
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
        let seconds = at.saturating_sub(open.start_ms()) as f64 / 1000.0;
        open.end(at);
        self.telemetry.record_duration(seconds, &attributes);
        for (kind, tokens) in usage
            .iter()
            .flat_map(|u| [("input", u.input_tokens), ("output", u.output_tokens)])
        {
            let mut attributes = attributes.clone();
            attributes.push(KeyValue::new(keys::TOKEN_TYPE, kind));
            self.telemetry.record_tokens(tokens, &attributes);
        }
    }
}
