//! The `Write` tool.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{JsonObject, Tool, ToolContext, ToolOutput, ToolResult, ToolScope};
use serde_json::json;

use crate::fs::ExecutionFs;
use crate::input::{schema, string};
use crate::tracker::ReadTracker;

/// Create or replace a whole file. Replacing an existing file needs a whole
/// `Read` of it at its current stamp. Scope `write`.
pub struct Write {
    fs: Arc<dyn ExecutionFs>,
    reads: Arc<ReadTracker>,
    schema: JsonObject,
}

impl Write {
    /// A `Write` over `fs` that checks and records through `reads`.
    pub fn new(fs: Arc<dyn ExecutionFs>, reads: Arc<ReadTracker>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "File to write, relative to the workspace root or absolute under it."
                },
                "content": {
                    "type": "string",
                    "description": "The complete new content of the file."
                }
            },
            "required": ["file_path", "content"]
        }));
        Self { fs, reads, schema }
    }
}

#[async_trait]
impl Tool for Write {
    fn name(&self) -> &str {
        "Write"
    }

    fn description(&self) -> &str {
        "Write UTF-8 text to a workspace file, creating parent directories. \
         An existing file must be Read in full first."
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
        format!("Write {path}")
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = string(&input, "file_path")?;
        let content = string(&input, "content")?;
        let target = self.fs.stat(path).await?;
        if let Some(stamp) = target.stamp {
            self.reads
                .check(&ctx.session_id, &target.path, stamp, None)?;
        }
        let stamp = self
            .fs
            .write(&target.path, content.as_bytes().to_vec(), target.stamp)
            .await?;
        self.reads
            .record_write(&ctx.session_id, &target.path, stamp);
        Ok(ToolOutput::new(format!("Wrote {}", target.path)))
    }
}
