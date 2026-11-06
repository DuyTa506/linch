//! The virtual filesystem tools: `ls`, `read_file`, `write_file`,
//! `edit_file`.
//!
//! They work on the session's [`super::FileBackend`], found through
//! [`SessionFiles`], never on the workspace. The names and path fields
//! (`path`, and `prefix` for `ls`) are the ones the permission engine's
//! built-in table checks against its virtual root.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::{
    JsonObject, Tool, ToolContext, ToolOutput, ToolOutputError, ToolResult, ToolScope,
};
use serde_json::json;

use super::{SessionFiles, VfsError, file_path};
use crate::input::{coded, flag, invalid, optional_count, optional_string, schema, string};
use crate::read::split_lines;

/// `ls`, `read_file`, `write_file` and `edit_file` over `files`.
pub fn vfs_tools(files: Arc<dyn SessionFiles>) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(Ls::new(files.clone())),
        Arc::new(ReadFile::new(files.clone())),
        Arc::new(WriteFile::new(files.clone())),
        Arc::new(EditFile::new(files)),
    ]
}

impl From<VfsError> for ToolOutputError {
    fn from(error: VfsError) -> Self {
        let code = match &error {
            VfsError::NotFound { path } => {
                let message = format!("File not found: '{path}'. Use ls() to see available files.");
                return coded(message, "not_found");
            }
            VfsError::InvalidPath { .. } => "invalid_path",
            VfsError::NoMatch { .. } => "no_match",
            VfsError::NotUnique { .. } => "ambiguous_match",
            VfsError::Backend(_) => "backend",
        };
        coded(error.to_string(), code)
    }
}

/// The trimmed `path` field, normalized as a file path.
fn path_field(input: &JsonObject) -> Result<String, ToolOutputError> {
    let path = string(input, "path")?.trim();
    if path.is_empty() {
        return Err(invalid("path must be a non-empty string"));
    }
    Ok(file_path(path)?)
}

fn path_of(input: &JsonObject) -> &str {
    input.get("path").and_then(|v| v.as_str()).unwrap_or("?")
}

fn plural(count: usize, one: &str) -> String {
    if count == 1 {
        format!("1 {one}")
    } else {
        format!("{count} {one}s")
    }
}

/// List virtual files, optionally under a prefix. Scope `read`.
pub struct Ls {
    files: Arc<dyn SessionFiles>,
    schema: JsonObject,
}

impl Ls {
    /// An `ls` over the session backends of `files`.
    pub fn new(files: Arc<dyn SessionFiles>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "prefix": {
                    "type": "string",
                    "description": "Optional path prefix to filter results, e.g. '/offload'."
                }
            }
        }));
        Self { files, schema }
    }
}

#[async_trait]
impl Tool for Ls {
    fn name(&self) -> &str {
        "ls"
    }

    fn description(&self) -> &str {
        "List files in the virtual filesystem. Use after a tool offloads a large \
         result to browse what is available, or to inspect your scratch notes. \
         Pass a prefix to narrow the listing."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        match input.get("prefix").and_then(|v| v.as_str()) {
            Some(prefix) if !prefix.is_empty() => format!("ls({prefix:?})"),
            _ => "ls()".to_owned(),
        }
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let prefix = optional_string(&input, "prefix")?.unwrap_or("");
        let paths = self.files.backend(&ctx.session_id).ls(prefix).await?;
        if paths.is_empty() {
            let message = match prefix {
                "" => "No files found.".to_owned(),
                prefix => format!("No files found under '{prefix}'."),
            };
            return Ok(ToolOutput::new(message));
        }
        Ok(ToolOutput::new(paths.join("\n")))
    }
}

/// Read a virtual file, optionally a window of its lines. Scope `read`.
pub struct ReadFile {
    files: Arc<dyn SessionFiles>,
    schema: JsonObject,
}

impl ReadFile {
    /// A `read_file` over the session backends of `files`.
    pub fn new(files: Arc<dyn SessionFiles>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Virtual file path, e.g. '/offload/web_search_abc.txt'."
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed line to start from (default 1)."
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum lines to return (default: entire file)."
                }
            },
            "required": ["path"]
        }));
        Self { files, schema }
    }
}

#[async_trait]
impl Tool for ReadFile {
    fn name(&self) -> &str {
        "read_file"
    }

    fn description(&self) -> &str {
        "Read a file from the virtual filesystem. Supports paged reading via offset \
         (1-indexed line number) and limit. Use this to inspect offloaded tool \
         results or files you wrote with write_file."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let mut summary = format!("read_file({:?}", path_of(input));
        for key in ["offset", "limit"] {
            if let Some(value) = input.get(key).and_then(|v| v.as_u64()) {
                summary.push_str(&format!(", {key}={value}"));
            }
        }
        summary.push(')');
        summary
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = path_field(&input)?;
        let offset = optional_count(&input, "offset")?;
        let limit = optional_count(&input, "limit")?;
        let text = self.files.backend(&ctx.session_id).read(&path).await?;
        if text.is_empty() {
            return Ok(ToolOutput::new("<file is empty>"));
        }
        if offset.is_none() && limit.is_none() {
            return Ok(ToolOutput::new(text));
        }
        let lines = split_lines(&text);
        let total = lines.len();
        let offset = offset.unwrap_or(1).max(1);
        let start = usize::try_from(offset - 1).unwrap_or(usize::MAX);
        if start >= total {
            return Ok(ToolOutput::new(format!(
                "<no lines at offset {offset}; {path} has {total} lines>"
            )));
        }
        let take = limit.map_or(total, |n| usize::try_from(n).unwrap_or(usize::MAX));
        let end = start.saturating_add(take).min(total);
        Ok(ToolOutput::new(lines[start..end].join("\n")))
    }
}

/// Create or replace a virtual file. Scope `write`.
pub struct WriteFile {
    files: Arc<dyn SessionFiles>,
    schema: JsonObject,
}

impl WriteFile {
    /// A `write_file` over the session backends of `files`.
    pub fn new(files: Arc<dyn SessionFiles>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Virtual file path to write."
                },
                "content": {"type": "string", "description": "File content."}
            },
            "required": ["path", "content"]
        }));
        Self { files, schema }
    }
}

#[async_trait]
impl Tool for WriteFile {
    fn name(&self) -> &str {
        "write_file"
    }

    fn description(&self) -> &str {
        "Write content to a virtual file. Creates the file if it does not exist, \
         or overwrites it entirely. Use as a scratchpad for notes, plans, and \
         intermediate results that you want to reference across turns."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("write_file({:?})", path_of(input))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = path_field(&input)?;
        let content = string(&input, "content")?;
        self.files
            .backend(&ctx.session_id)
            .write(&path, content)
            .await?;
        let lines = plural(split_lines(content).len(), "line");
        Ok(ToolOutput::new(format!("Written {lines} to '{path}'.")))
    }
}

/// Replace exact text in a virtual file. Scope `write`.
pub struct EditFile {
    files: Arc<dyn SessionFiles>,
    schema: JsonObject,
}

impl EditFile {
    /// An `edit_file` over the session backends of `files`.
    pub fn new(files: Arc<dyn SessionFiles>) -> Self {
        let schema = schema(json!({
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "description": "Virtual file path."},
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact text to find and replace."
                },
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false: error if not unique)."
                }
            },
            "required": ["path", "old_string", "new_string"]
        }));
        Self { files, schema }
    }
}

#[async_trait]
impl Tool for EditFile {
    fn name(&self) -> &str {
        "edit_file"
    }

    fn description(&self) -> &str {
        "Replace an exact string in a virtual file. The old_string must match the \
         file contents exactly (including indentation). Use replace_all to \
         substitute every occurrence. Returns the replacement count."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Write
    }

    fn summarize(&self, input: &JsonObject) -> String {
        format!("edit_file({:?})", path_of(input))
    }

    async fn execute(&self, input: JsonObject, ctx: ToolContext) -> ToolResult {
        let path = path_field(&input)?;
        let old = string(&input, "old_string")?;
        let new = string(&input, "new_string")?;
        let all = flag(&input, "replace_all")?;
        if old.is_empty() {
            return Err(invalid("old_string must not be empty"));
        }
        let backend = self.files.backend(&ctx.session_id);
        let count = backend.edit(&path, old, new, all).await?;
        let made = plural(count, "replacement");
        Ok(ToolOutput::new(format!("Made {made} in '{path}'.")))
    }
}
