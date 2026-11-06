//! Tool calls: an `execute_tool` span from each start to its end.

use linch_core::events::kinds::{ToolCallEndEvent, ToolCallStartEvent};
use linch_core::{ToolResultBlock, ToolResultPart};
use opentelemetry::trace::SpanKind;
use opentelemetry::{Context, KeyValue};

use super::RunObserver;
use crate::keys;
use crate::span::{Open, count, json};

impl RunObserver {
    pub(super) fn open_tool(&self, parent: &Context, name: &str, id: &str, at: u64) -> Open {
        let mut attributes = vec![KeyValue::new(keys::OPERATION, keys::EXECUTE_TOOL)];
        if let Some(session_id) = &self.session_id {
            attributes.push(KeyValue::new(keys::CONVERSATION_ID, session_id.clone()));
        }
        attributes.push(KeyValue::new(keys::TOOL_NAME, name.to_owned()));
        attributes.push(KeyValue::new(keys::TOOL_CALL_ID, id.to_owned()));
        if let Some(run_id) = &self.run_id {
            attributes.push(KeyValue::new(keys::RUN_ID, run_id.clone()));
        }
        if let Some(turn) = self.turn {
            attributes.push(KeyValue::new(keys::TURN_INDEX, count(turn)));
        }
        let span_name = format!("{} {name}", keys::EXECUTE_TOOL);
        self.start(parent, span_name, SpanKind::Internal, at, attributes)
    }

    pub(super) fn tool_start(&mut self, start: &ToolCallStartEvent, at: u64) {
        let (step, _) = self.step(at);
        let id = start.tool_use_id.as_str();
        if let Some(index) = self.tools.iter().position(|(tool_id, _)| tool_id == id) {
            self.tools.remove(index).1.end(at);
        }
        let open = self.open_tool(&step, &start.tool_name, id, at);
        if self.capture && open.recording() {
            open.set(KeyValue::new(keys::TOOL_ARGUMENTS, json(&start.input)));
        }
        self.tools.push((id.to_owned(), open));
    }

    pub(super) fn tool_end(&mut self, end: &ToolCallEndEvent, at: u64) {
        let (step, _) = self.step(at);
        let id = end.tool_use_id();
        let (open, error) = match self.tools.iter().position(|(tool_id, _)| tool_id == id) {
            Some(index) => (self.tools.remove(index).1, "tool_error"),
            // Its start is in an earlier stream: the placeholder end of a
            // tool a crash interrupted.
            None => {
                let start = at.saturating_sub(end.duration_ms);
                (
                    self.open_tool(&step, &end.tool_name, id, start),
                    "interrupted",
                )
            }
        };
        if end.is_error() {
            open.set_error(error);
        }
        if self.capture && open.recording() {
            open.set(KeyValue::new(keys::TOOL_RESULT, result_text(&end.result)));
        }
        open.end(at);
    }
}

/// A tool result as Python's `str(content)` gives a text result: its text,
/// or the content's JSON when it holds an image.
fn result_text(block: &ToolResultBlock) -> String {
    let mut text = String::new();
    for part in &block.content {
        match part {
            ToolResultPart::Text(part) => text.push_str(&part.text),
            ToolResultPart::Image(_) => return json(&block.content),
        }
    }
    text
}
