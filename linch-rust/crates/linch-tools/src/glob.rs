//! The `Glob` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{JsonObject, Tool, ToolContext, ToolOutput, ToolResult, ToolScope};
use serde_json::json;

use crate::fs::{ExecutionFs, GlobQuery};
use crate::input::{optional_string, schema, string};

/// Most paths returned.
const MAX_RESULTS: usize = 1000;

/// Find files by name pattern, newest first. Scope `read`.
pub struct Glob {
    fs: Arc<dyn ExecutionFs>,
    schema: JsonObject,
}

impl Glob {
    /// A `Glob` over `fs`.
    pub fn new(fs: Arc<dyn ExecutionFs>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Glob with ripgrep --glob rules, e.g. '*.rs' or 'src/**/*.ts'."
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search. Default: the workspace root."
                }
            },
            "required": ["pattern"]
        }));
        Self { fs, schema }
    }
}

#[async_trait]
impl Tool for Glob {
    fn name(&self) -> &str {
        "Glob"
    }

    fn description(&self) -> &str {
        "Find files by glob pattern. A pattern without '/' matches file names at any \
         depth ('*.rs'); one with '/' is anchored to the search directory \
         ('src/**/*.ts'). Skips gitignored files, and hidden ones unless the pattern \
         names them. Newest first, at most 1000 paths."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let pattern = input.get("pattern").and_then(|v| v.as_str()).unwrap_or("");
        format!("Glob {pattern}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let query = GlobQuery {
            pattern: string(&input, "pattern")?.to_owned(),
            path: optional_string(&input, "path")?.map(str::to_owned),
        };
        let mut hits = self.fs.glob(&query, &ctx.cancel).await?;
        if hits.is_empty() {
            return Ok(ToolOutput::new("No matches found."));
        }
        hits.sort_by(|a, b| {
            b.modified
                .cmp(&a.modified)
                .then_with(|| a.path.cmp(&b.path))
        });
        let total = hits.len();
        let mut text = hits
            .iter()
            .take(MAX_RESULTS)
            .map(|hit| hit.path.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        if total > MAX_RESULTS {
            text.push_str(&format!(
                "\n… (truncated to {MAX_RESULTS} of {total} results)"
            ));
        }
        Ok(ToolOutput::new(text))
    }
}
