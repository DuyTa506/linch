//! The `Edit` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{JsonObject, Tool, ToolContext, ToolOutput, ToolResult, ToolScope};
use serde_json::json;

use crate::fs::ExecutionFs;
use crate::input::{coded, flag, invalid, schema, string};
use crate::read::MAX_READ_BYTES;
use crate::tracker::ReadTracker;

/// Replace exact text in a file. The file must have been read at its
/// current stamp, including every line a replaced match touches. Scope
/// `write`.
pub struct Edit {
    fs: Arc<dyn ExecutionFs>,
    reads: Arc<ReadTracker>,
    schema: JsonObject,
}

impl Edit {
    /// An `Edit` over `fs` that checks and records through `reads`.
    pub fn new(fs: Arc<dyn ExecutionFs>, reads: Arc<ReadTracker>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "File to edit, relative to the workspace root or absolute under it."
                },
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact text to replace."
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of exactly one. Default false."
                }
            },
            "required": ["file_path", "old_string", "new_string"]
        }));
        Self { fs, reads, schema }
    }
}

#[async_trait]
impl Tool for Edit {
    fn name(&self) -> &str {
        "Edit"
    }

    fn description(&self) -> &str {
        "Replace exact text in a workspace file. old_string must match exactly once \
         unless replace_all is true. Read the file first."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let path = input
            .get("file_path")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        format!("Edit {path}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = string(&input, "file_path")?;
        let old = string(&input, "old_string")?;
        let new = string(&input, "new_string")?;
        let all = flag(&input, "replace_all")?;
        if old.is_empty() {
            return Err(invalid(
                "old_string must not be empty; use Write to create or replace a whole file",
            ));
        }
        let file = self.fs.read(path, MAX_READ_BYTES).await?;
        let session = &ctx.session_id;
        self.reads
            .check(session, &file.path, file.stamp, Some(&[]))?;
        let text = String::from_utf8(file.bytes)
            .map_err(|_| coded(format!("{} is not UTF-8 text.", file.path), "not_text"))?;
        let starts: Vec<usize> = text.match_indices(old).map(|(at, _)| at).collect();
        match starts.len() {
            0 => {
                return Err(coded(
                    format!("old_string was not found in {}.", file.path),
                    "no_match",
                ));
            }
            1 => {}
            n if !all => {
                return Err(coded(
                    format!(
                        "old_string appears {n} times in {}; give a unique match or set replace_all.",
                        file.path
                    ),
                    "ambiguous_match",
                ));
            }
            _ => {}
        }
        let spans = line_spans(&text, old, &starts);
        self.reads
            .check(session, &file.path, file.stamp, Some(&spans))?;
        let updated = text.replace(old, new);
        let stamp = self
            .fs
            .write(&file.path, updated.into_bytes(), Some(file.stamp))
            .await?;
        self.reads.record_edit(session, &file.path, stamp);
        let message = match starts.len() {
            1 => format!("Edited {}", file.path),
            n => format!("Edited {} ({n} replacements)", file.path),
        };
        Ok(ToolOutput::new(message))
    }
}

/// The inclusive 1-based lines each match of `old` at `starts` touches.
fn line_spans(text: &str, old: &str, starts: &[usize]) -> Vec<(u64, u64)> {
    let inner = old.matches('\n').count() as u64 - u64::from(old.ends_with('\n'));
    let mut line = 1;
    let mut seen = 0;
    starts
        .iter()
        .map(|&at| {
            line += text[seen..at].matches('\n').count() as u64;
            seen = at;
            (line, line + inner)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::line_spans;

    #[test]
    fn spans_count_lines_before_and_inside_each_match() {
        let text = "a\nfoo\nb\nfoo\nbar\n";
        let starts: Vec<usize> = text.match_indices("foo").map(|(at, _)| at).collect();
        assert_eq!(line_spans(text, "foo", &starts), [(2, 2), (4, 4)]);
        let starts: Vec<usize> = text.match_indices("foo\nbar").map(|(at, _)| at).collect();
        assert_eq!(line_spans(text, "foo\nbar", &starts), [(4, 5)]);
        // A trailing newline does not reach into the next line.
        let starts: Vec<usize> = text.match_indices("b\n").map(|(at, _)| at).collect();
        assert_eq!(line_spans(text, "b\n", &starts), [(3, 3)]);
    }
}
