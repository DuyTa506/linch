//! The `Edit` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{JsonObject, Tool, ToolContext, ToolOutput, ToolResult, ToolScope};
use serde_json::json;

use crate::fs::ExecutionFs;
use crate::input::{coded, flag, schema, string};
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
        let file = self.fs.read(path, MAX_READ_BYTES).await?;
        let session = &ctx.session_id;
        self.reads
            .check(session, &file.path, file.stamp, Some(&[]))
            .map_err(|error| error.doing("editing"))?;
        let text = String::from_utf8(file.bytes)
            .map_err(|_| coded(format!("{} is not UTF-8 text.", file.path), "not_text"))?;
        if all && old.is_empty() {
            return Err(coded(
                "old_string must not be empty with replace_all.",
                "empty_old_string",
            ));
        }
        if old.is_empty() {
            // An empty `old` matches at every position, as in Python, so the
            // first line must have been read.
            self.reads
                .check(session, &file.path, file.stamp, Some(&[(1, 1)]))
                .map_err(|error| error.doing("editing"))?;
        }
        // Count the matches before anything is built from them: a one-byte
        // `old` in a file at the read limit matches ten million times.
        let count = text.matches(old).count();
        match count {
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
        // The matches never overlap, so they take `count * old.len()` bytes
        // of `text`: the size is known before the result is built.
        let kept = text.len() - count * old.len();
        let size = (count.checked_mul(new.len()))
            .and_then(|added| kept.checked_add(added))
            .unwrap_or(usize::MAX);
        if size as u64 > MAX_READ_BYTES {
            return Err(coded(
                format!(
                    "The edit would make {} larger than {MAX_READ_BYTES} bytes; use a shorter new_string.",
                    file.path
                ),
                "too_large",
            ));
        }
        let spans = line_spans(&text, old);
        self.reads
            .check(session, &file.path, file.stamp, Some(&spans))
            .map_err(|error| error.doing("editing"))?;
        let updated = text.replace(old, new);
        let stamp = self
            .fs
            .write(&file.path, updated.into_bytes(), Some(file.stamp))
            .await?;
        self.reads.record_edit(session, &file.path, stamp);
        let message = match count {
            1 => format!("Edited {}", file.path),
            n => format!("Edited {} ({n} replacements)", file.path),
        };
        Ok(ToolOutput::new(message))
    }
}

/// The inclusive 1-based lines the matches of `old` in `text` touch, with
/// ranges that overlap or touch joined: a file of ten million one-byte
/// matches holds one range, not ten million. The read record merges its
/// ranges the same way, so the joined ranges are covered exactly when the
/// separate ones are.
fn line_spans(text: &str, old: &str) -> Vec<(u64, u64)> {
    let inner = old.matches('\n').count() as u64 - u64::from(old.ends_with('\n'));
    let mut line = 1;
    let mut seen = 0;
    let mut spans: Vec<(u64, u64)> = Vec::new();
    for (at, _) in text.match_indices(old) {
        line += text[seen..at].matches('\n').count() as u64;
        seen = at;
        let span = (line, line + inner);
        match spans.last_mut() {
            Some(last) if span.0 <= last.1.saturating_add(1) => last.1 = last.1.max(span.1),
            _ => spans.push(span),
        }
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::line_spans;

    #[test]
    fn spans_count_lines_before_and_inside_each_match() {
        let text = "a\nfoo\nb\nfoo\nbar\n";
        assert_eq!(line_spans(text, "foo"), [(2, 2), (4, 4)]);
        assert_eq!(line_spans(text, "foo\nbar"), [(4, 5)]);
        // A trailing newline does not reach into the next line.
        assert_eq!(line_spans(text, "b\n"), [(3, 3)]);
    }

    #[test]
    fn spans_that_overlap_or_touch_are_joined() {
        // Lines 1, 2 and 3 touch; line 5 does not.
        assert_eq!(line_spans("x\nx\nx\n\nx\n", "x"), [(1, 3), (5, 5)]);
        // Two matches of one line are one range.
        assert_eq!(line_spans("xx\n", "x"), [(1, 1)]);
        // Matches over lines 1-2 and 3-4 touch.
        assert_eq!(line_spans("a\nb\na\nb", "a\nb"), [(1, 4)]);
    }
}
