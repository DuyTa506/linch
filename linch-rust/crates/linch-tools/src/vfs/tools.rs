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
use linch_core::vfs::check_size;

/// System prompt text that teaches the model the virtual filesystem and the
/// offload pointers. Python adds it as a cacheable block whenever one of
/// the four tools is offered.
pub const PROMPT: &str = "Virtual filesystem:\n\n\
    - You have a virtual filesystem, separate from the real workspace, accessed via \
    ls, read_file, write_file, and edit_file.\n\
    - Large tool results may be automatically offloaded here: instead of the full \
    output you will see a short preview plus a file path. Call read_file(path, \
    offset, limit) to read the parts you need; do not assume the preview is the \
    whole result.\n\
    - Use write_file as a scratchpad for notes, plans, or intermediate results you \
    want to keep across turns without bloating the conversation.";

/// `ls`, `read_file`, `write_file` and `edit_file` over `files`.
pub fn vfs_tools(files: Arc<dyn SessionFiles>) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(Ls::new(files.clone())),
        Arc::new(ReadFile::new(files.clone())),
        Arc::new(WriteFile::new(files.clone())),
        Arc::new(EditFile::new(files)),
    ]
}

/// The model-facing error for a failed file operation, with a code.
fn tool_error(error: VfsError) -> ToolOutputError {
    let code = match &error {
        VfsError::NotFound { path } => {
            let message = format!("File not found: '{path}'. Use ls() to see available files.");
            return coded(message, "not_found");
        }
        VfsError::InvalidPath { .. } => "invalid_path",
        VfsError::NoMatch { .. } => "no_match",
        VfsError::NotUnique { .. } => "ambiguous_match",
        VfsError::TooLarge { .. } => "too_large",
        VfsError::Backend(_) => "backend",
    };
    coded(error.to_string(), code)
}

/// [`tool_error`], naming `path` when the file would be too large.
fn write_error(path: &str, error: VfsError) -> ToolOutputError {
    let VfsError::TooLarge { size, limit } = error else {
        return tool_error(error);
    };
    let message = format!(
        "'{path}' would be {size} bytes, past the {limit} bytes a virtual file holds; \
         nothing was changed. Write less, or split it into several files."
    );
    coded(message, "too_large")
}

/// The trimmed `path` field, normalized as a file path.
fn path_field(input: &JsonObject) -> Result<String, ToolOutputError> {
    let path = string(input, "path")?.trim();
    if path.is_empty() {
        return Err(invalid("path must be a non-empty string"));
    }
    file_path(path).map_err(tool_error)
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
        let paths = (self.files.backend(&ctx.session_id).ls(prefix).await).map_err(tool_error)?;
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
                    "description": "Virtual file path, e.g. '/offload/s1/web_search_abc_3fa9c1d2e4b5a678.txt'."
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
                },
                "byte_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte to start from inside the line at offset; only with \
                                    limit=1. A line longer than 16384 bytes is cut and ends \
                                    with the exact call that continues it."
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
         (1-indexed line number) and limit; a line longer than 16384 bytes is cut \
         and ends with the call (byte_offset) that continues it. Use this to inspect \
         offloaded tool results or files you wrote with write_file."
    }

    fn input_schema(&self) -> &JsonObject {
        &self.schema
    }

    fn scope(&self) -> ToolScope {
        ToolScope::Read
    }

    fn summarize(&self, input: &JsonObject) -> String {
        let mut summary = format!("read_file({:?}", path_of(input));
        for key in ["offset", "limit", "byte_offset"] {
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
        let byte_offset = optional_count(&input, "byte_offset")?;
        if byte_offset.is_some() && limit != Some(1) {
            return Err(invalid(
                "byte_offset continues one line: pass limit=1 and the line's offset",
            ));
        }
        let text = (self.files.backend(&ctx.session_id).read(&path).await).map_err(tool_error)?;
        if text.is_empty() {
            return Ok(ToolOutput::new("<file is empty>"));
        }
        let whole = offset.is_none() && limit.is_none();
        // No line can be longer than the text, so a short file is read as it is.
        if whole && text.len() <= LINE_PAGE_BYTES {
            return Ok(ToolOutput::new(text));
        }
        let lines = split_lines(&text);
        if whole && lines.iter().all(|line| line.len() <= LINE_PAGE_BYTES) {
            return Ok(ToolOutput::new(text));
        }
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
        let from = byte_offset.map_or(0, |at| usize::try_from(at).unwrap_or(usize::MAX));
        let mut shown = Vec::with_capacity(end - start);
        for (at, line) in lines[start..end].iter().enumerate() {
            // `byte_offset` needs `limit=1`, so it only ever meets the first line.
            let from = if at == 0 { from } else { 0 };
            shown.push(page_line(line, from, start + at + 1, &path)?);
        }
        Ok(ToolOutput::new(shown.join("\n")))
    }
}

/// Most bytes of one line a `read_file` call returns: a longer line is cut
/// there (on a character boundary) and ends with the call that continues
/// it, so one minified line never comes back whole.
const LINE_PAGE_BYTES: usize = 16 * 1024;

/// Line number `number` of `path`, from byte `from`, at most
/// [`LINE_PAGE_BYTES`] of it. A cut ends with the exact continuation call.
fn page_line(
    line: &str,
    from: usize,
    number: usize,
    path: &str,
) -> Result<String, ToolOutputError> {
    if from > 0 && from >= line.len() {
        return Ok(format!(
            "<no bytes at byte_offset {from}; line {number} has {} bytes>",
            line.len()
        ));
    }
    if !line.is_char_boundary(from) {
        return Err(invalid(format!(
            "byte_offset {from} is inside a character of line {number}; \
             use the byte_offset a previous read_file reported"
        )));
    }
    let rest = &line[from..];
    if rest.len() <= LINE_PAGE_BYTES {
        return Ok(rest.to_owned());
    }
    let mut end = LINE_PAGE_BYTES;
    while !rest.is_char_boundary(end) {
        end -= 1;
    }
    let next = from + end;
    Ok(format!(
        "{}\n[line {number} continues at byte {next}; read_file(path=\"{path}\", \
         offset={number}, limit=1, byte_offset={next})]",
        &rest[..end]
    ))
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
        check_size(content.len()).map_err(|error| write_error(&path, error))?;
        self.files
            .backend(&ctx.session_id)
            .write(&path, content)
            .await
            .map_err(tool_error)?;
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
        let count = backend
            .edit(&path, old, new, all)
            .await
            .map_err(|error| write_error(&path, error))?;
        let made = plural(count, "replacement");
        Ok(ToolOutput::new(format!("Made {made} in '{path}'.")))
    }
}
