//! The `Read` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{JsonObject, Tool, ToolContext, ToolOutput, ToolResult, ToolScope};
use serde_json::json;

use crate::fs::ExecutionFs;
use crate::input::{coded, optional_count, schema, string};
use crate::tracker::{ReadTracker, Span};

/// Largest file `Read` and `Edit` load, in bytes.
pub(crate) const MAX_READ_BYTES: u64 = 10 * 1024 * 1024;

/// Lines returned when the call gives no `limit`.
const DEFAULT_LIMIT: u64 = 2000;

/// Read a UTF-8 text file with numbered lines, recording what was seen for
/// the read-before-write policy. Scope `read`.
pub struct Read {
    fs: Arc<dyn ExecutionFs>,
    reads: Arc<ReadTracker>,
    schema: JsonObject,
}

impl Read {
    /// A `Read` over `fs` that records into `reads`.
    pub fn new(fs: Arc<dyn ExecutionFs>, reads: Arc<ReadTracker>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "File to read, relative to the workspace root or absolute under it."
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-based). Default 1."
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Most lines to return. Default 2000."
                }
            },
            "required": ["file_path"]
        }));
        Self { fs, reads, schema }
    }
}

#[async_trait]
impl Tool for Read {
    fn name(&self) -> &str {
        "Read"
    }

    fn description(&self) -> &str {
        "Read a UTF-8 text file from the workspace. Each line is prefixed with its \
         1-based number and a tab. Use offset and limit to read a window of a large \
         file (default: the first 2000 lines)."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let path = input
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        format!("Read {path}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = string(&input, "file_path")?;
        let offset = optional_count(&input, "offset")?.unwrap_or(1).max(1);
        let limit = optional_count(&input, "limit")?.unwrap_or(DEFAULT_LIMIT);
        let file = self.fs.read(path, MAX_READ_BYTES).await?;
        if file.bytes.contains(&0) {
            return Err(coded(
                format!("{} is a binary file and cannot be read as text.", file.path),
                "binary",
            ));
        }
        let text = String::from_utf8(file.bytes)
            .map_err(|_| coded(format!("{} is not UTF-8 text.", file.path), "not_text"))?;
        let lines = split_lines(&text);
        let total = lines.len() as u64;
        let start = (offset - 1).min(total);
        let end = start.saturating_add(limit).min(total);
        if start < end {
            let span = if start == 0 && end == total {
                Span::Whole
            } else {
                Span::Lines(start + 1, end)
            };
            self.reads
                .record_read(&ctx.session_id, &file.path, file.stamp, span);
        } else if total == 0 {
            // Seeing an empty file is seeing all of it.
            self.reads
                .record_read(&ctx.session_id, &file.path, file.stamp, Span::Whole);
            return Ok(ToolOutput::new("<file is empty>"));
        } else {
            return Ok(ToolOutput::new(format!(
                "<no lines at offset {offset}; {} has {total} lines>",
                file.path
            )));
        }
        let numbered: Vec<String> = (start..end)
            .zip(&lines[start as usize..end as usize])
            .map(|(index, line)| format!("{}\t{line}", index + 1))
            .collect();
        Ok(ToolOutput::new(numbered.join("\n")))
    }
}

/// The lines of `text`; a final newline does not start another line.
pub(crate) fn split_lines(text: &str) -> Vec<&str> {
    if text.is_empty() {
        return Vec::new();
    }
    let body = text.strip_suffix('\n').unwrap_or(text);
    body.split('\n').collect()
}
