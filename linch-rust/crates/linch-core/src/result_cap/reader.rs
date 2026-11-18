//! `ReadToolResult`: read a slice of a tool result the session logged.
//!
//! The marker of a cut result names this tool and the byte offset where the
//! cut began. The tool reads the result from the session's durable log (the
//! audit entries hold every byte), so it works after a resume and in a
//! process that never saw the call, and keeps nothing of its own.

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::json;

use super::{READ_TOOL, ToolResultCap, joined_text};
use crate::ids::SessionId;
use crate::json::{JsonObject, JsonValue};
use crate::message::ContentBlock;
use crate::session_log::LogEntry;
use crate::store::SessionStore;
use crate::tool::{Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope};

const DESCRIPTION: &str = "Read part of an earlier tool result that was cut from your view.

A cut result reads `[... N bytes omitted; read them with ReadToolResult(tool_use_id=\"...\", \
offset=...) ...]`. Pass that `tool_use_id` and `offset` (a byte offset into the result's \
text) to read the next part; every part ends with the `offset` to continue from, or says \
the result ended. 'limit' is the most bytes to return and is capped; a smaller one reads \
less.";

/// Bytes of an id the tool echoes in an error.
const ID_ECHO: usize = 64;

/// Reads a slice of a logged tool result of the calling session.
///
/// Read scope, so its calls are never held for approval and may run beside
/// other reads. A slice is at most [`ReadToolResultTool::max_limit`] bytes
/// and its footer is small, so it stays under the cap that cut the result
/// and is never cut again.
pub(crate) struct ReadToolResultTool {
    sessions: Arc<dyn SessionStore>,
    namespace: String,
    max_limit: usize,
    schema: JsonObject,
}

impl ReadToolResultTool {
    /// A tool that reads from `sessions` in `namespace`, with slices that
    /// fit under `cap`.
    pub(crate) fn new(
        sessions: Arc<dyn SessionStore>,
        namespace: impl Into<String>,
        cap: &ToolResultCap,
    ) -> Self {
        let budget = cap.budget();
        // Room for the footer under the cap: all of it for a large cap, half
        // of a small one.
        let max_limit = budget - (budget / 2).min(512);
        let schema = json!({
            "type": "object",
            "properties": {
                "tool_use_id": {
                    "type": "string",
                    "description": "The tool_use_id named in the marker of the cut result.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte offset to start at (default 0). The marker and each \
                                    part name the next one.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": format!(
                        "Most bytes to return (default and most: {max_limit})."
                    ),
                },
            },
            "required": ["tool_use_id"],
        });
        let JsonValue::Object(schema) = schema else {
            unreachable!("the schema is an object")
        };
        Self {
            sessions,
            namespace: namespace.into(),
            max_limit,
            schema,
        }
    }

    /// The text of the latest result of the call `tool_use_id` in
    /// `session`, from the audit entries: ids repeat across runs of a
    /// session on some providers, and the latest is the one a marker in the
    /// view can name.
    async fn full_text(
        &self,
        session: &SessionId,
        tool_use_id: &str,
    ) -> Result<Option<String>, ToolOutputError> {
        let entries = (self.sessions.load(&self.namespace, session).await)
            .map_err(|_| ToolOutputError::new("the session's log could not be read"))?;
        let results = entries.iter().rev().filter_map(|entry| match entry {
            LogEntry::Message(entry) if entry.historical => Some(&entry.message),
            _ => None,
        });
        for message in results {
            for block in &message.content {
                if let ContentBlock::ToolResult(result) = block
                    && result.tool_use_id == tool_use_id
                {
                    return Ok(Some(joined_text(result)));
                }
            }
        }
        Ok(None)
    }
}

/// The bytes `start..end` of `text` that `offset` and `limit` ask for, each
/// end moved back to a character boundary; at least one character when
/// `start` is inside the text.
fn window(text: &str, offset: usize, limit: usize) -> (usize, usize) {
    let len = text.len();
    let mut start = offset.min(len);
    while !text.is_char_boundary(start) {
        start -= 1;
    }
    let mut end = start.saturating_add(limit).min(len);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    while end == start && end < len {
        end += 1;
        while !text.is_char_boundary(end) {
            end += 1;
        }
    }
    (start, end)
}

fn number(input: &JsonObject, field: &str) -> Option<usize> {
    let value = input.get(field)?.as_u64()?;
    Some(usize::try_from(value).unwrap_or(usize::MAX))
}

#[async_trait]
impl Tool for ReadToolResultTool {
    fn name(&self) -> &str {
        READ_TOOL
    }

    fn description(&self) -> &str {
        DESCRIPTION
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    /// Offered even when tool search defers the others: a marker in the
    /// view names it.
    fn always_load(&self) -> bool {
        true
    }

    /// Pure reading of a log that only grows: running it again is the same.
    fn idempotent(&self) -> bool {
        true
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let id = input.get("tool_use_id").and_then(JsonValue::as_str);
        format!("Read tool result {}", id.unwrap_or(""))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let id = (input.get("tool_use_id").and_then(JsonValue::as_str)).unwrap_or("");
        let offset = number(&input, "offset").unwrap_or(0);
        let limit = number(&input, "limit").unwrap_or(self.max_limit);
        let limit = limit.clamp(1, self.max_limit);
        let Some(text) = self.full_text(&ctx.session_id, id).await? else {
            let shown: String = id.chars().take(ID_ECHO).collect();
            return Err(ToolOutputError::new(format!(
                "no tool result with tool_use_id \"{shown}\" in this session"
            )));
        };
        let total = text.len();
        if offset >= total && total > 0 {
            return Err(ToolOutputError::new(format!(
                "offset {offset} is past the end of the result ({total} bytes)"
            )));
        }
        let (start, end) = window(&text, offset, limit);
        let slice = &text[start..end];
        let footer = if end < total {
            format!(
                "\n[... bytes {start}..{end} of {total}; read on with {READ_TOOL}(tool_use_id=\"{id}\", \
                 offset={end}) ...]"
            )
        } else {
            format!("\n[... bytes {start}..{end} of {total}; end of the result ...]")
        };
        let mut output = ToolOutput::new(format!("{slice}{footer}"));
        output
            .metadata
            .insert("total_bytes".into(), (total as u64).into());
        output
            .metadata
            .insert("offset".into(), (start as u64).into());
        output
            .metadata
            .insert("next_offset".into(), (end as u64).into());
        Ok(output)
    }
}

#[cfg(test)]
mod tests;
