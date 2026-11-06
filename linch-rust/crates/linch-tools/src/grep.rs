//! The `Grep` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{
    JsonObject, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

use crate::fs::{ExecutionFs, GrepHits, GrepMode, GrepQuery};
use crate::input::{flag, invalid, optional_count, optional_string, schema, string};

/// Entries returned when the call gives no `head_limit`.
const DEFAULT_HEAD_LIMIT: u64 = 250;

/// Largest output, in bytes.
const OUTPUT_CAP: usize = 30_000;

/// Most entries, and context lines on each side, a call may ask for. Every
/// entry takes at least two bytes of output, so more could never be shown.
const MAX_LINES: u64 = (OUTPUT_CAP / 2) as u64;

/// Search file contents by regex with ripgrep's engine, in-process. Scope
/// `read`.
pub struct Grep {
    fs: Arc<dyn ExecutionFs>,
    schema: JsonObject,
}

impl Grep {
    /// A `Grep` over `fs`.
    pub fn new(fs: Arc<dyn ExecutionFs>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "Regex to search for (Rust regex syntax)."},
                "path": {"type": "string", "description": "File or directory to search. Default: the workspace root."},
                "glob": {"type": "string", "description": "File filter with ripgrep --glob rules, e.g. '*.ts'."},
                "type": {"type": "string", "description": "ripgrep file type, e.g. rust, py, ts, js, go."},
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content", "count"],
                    "description": "Default files_with_matches."
                },
                "-i": {"type": "boolean", "description": "Ignore case."},
                "-n": {"type": "boolean", "description": "Show line numbers (content mode)."},
                "-A": {"type": "integer", "minimum": 0, "description": "Context lines after each match (content mode)."},
                "-B": {"type": "integer", "minimum": 0, "description": "Context lines before each match (content mode)."},
                "-C": {"type": "integer", "minimum": 0, "description": "Context lines around each match (content mode)."},
                "multiline": {"type": "boolean", "description": "Let a match span lines; '.' also matches a newline."},
                "head_limit": {"type": "integer", "minimum": 1, "description": "Most files, or lines in content mode. Default 250."}
            },
            "required": ["pattern"]
        }));
        Self { fs, schema }
    }
}

#[async_trait]
impl Tool for Grep {
    fn name(&self) -> &str {
        "Grep"
    }

    fn description(&self) -> &str {
        "Search file contents by regex. Skips hidden, gitignored and binary files. \
         output_mode is files_with_matches (default), content or count; content mode \
         takes -n and -A/-B/-C context. glob and type filter files; head_limit caps \
         the entries returned (default 250)."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let pattern = input.get("pattern").and_then(|v| v.as_str()).unwrap_or("");
        format!("Grep {pattern}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let pattern = string(&input, "pattern")?;
        if pattern.is_empty() {
            return Err(invalid("pattern must not be empty"));
        }
        let mode = match optional_string(&input, "output_mode")? {
            None | Some("files_with_matches") => GrepMode::FilesWithMatches,
            Some("content") => GrepMode::Content,
            Some("count") => GrepMode::Count,
            Some(other) => return Err(invalid(format!("unknown output_mode {other:?}"))),
        };
        let around = optional_count(&input, "-C")?.unwrap_or(0);
        let side = |key: &str| -> Result<usize, ToolOutputError> {
            let lines = if around > 0 {
                around
            } else {
                optional_count(&input, key)?.unwrap_or(0)
            };
            Ok(usize::try_from(lines.min(MAX_LINES)).unwrap_or(usize::MAX))
        };
        let limit = optional_count(&input, "head_limit")?
            .unwrap_or(DEFAULT_HEAD_LIMIT)
            .clamp(1, MAX_LINES);
        let query = GrepQuery {
            pattern: pattern.to_owned(),
            path: optional_string(&input, "path")?.map(str::to_owned),
            glob: optional_string(&input, "glob")?.map(str::to_owned),
            file_type: optional_string(&input, "type")?.map(str::to_owned),
            mode,
            case_insensitive: flag(&input, "-i")?,
            multiline: flag(&input, "multiline")?,
            before: side("-B")?,
            after: side("-A")?,
            limit: usize::try_from(limit).unwrap_or(usize::MAX),
        };
        let hits = self.fs.grep(&query, &ctx.cancel).await?;
        if hits.files.is_empty() {
            return Ok(ToolOutput::new("No matches found."));
        }
        let mut text = render(&hits, &query, flag(&input, "-n")?);
        if text.len() > OUTPUT_CAP {
            let mut cut = OUTPUT_CAP;
            while !text.is_char_boundary(cut) {
                cut -= 1;
            }
            text.truncate(cut);
            text.push_str(&format!("\n… (output capped at {OUTPUT_CAP} bytes)"));
        }
        Ok(ToolOutput::new(text))
    }
}

/// ripgrep-style text: `path`, `path:count`, or `path:line` (`path-line` for
/// context) with `--` between groups of context.
fn render(hits: &GrepHits, query: &GrepQuery, numbered: bool) -> String {
    let mut out: Vec<String> = Vec::new();
    for file in &hits.files {
        match query.mode {
            GrepMode::FilesWithMatches => out.push(file.path.clone()),
            GrepMode::Count => out.push(format!("{}:{}", file.path, file.count)),
            GrepMode::Content => {
                let context = query.before > 0 || query.after > 0;
                let mut previous: Option<u64> = None;
                for line in &file.lines {
                    let gap = previous.map_or(!out.is_empty(), |p| line.number > p + 1);
                    if context && gap {
                        out.push("--".to_owned());
                    }
                    let sep = if line.is_match { ':' } else { '-' };
                    out.push(if numbered {
                        format!("{}{sep}{}{sep}{}", file.path, line.number, line.text)
                    } else {
                        format!("{}{sep}{}", file.path, line.text)
                    });
                    previous = Some(line.number);
                }
            }
        }
    }
    if hits.truncated {
        out.push(format!("… (truncated at head_limit {})", query.limit));
    }
    out.join("\n")
}
