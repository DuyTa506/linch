//! [`ResultOffload`]: move oversized tool results into the virtual
//! filesystem, as a `post_tool_use` hook.
//!
//! When a tool result is estimated above [`OffloadConfig::threshold_tokens`],
//! the hook writes the whole text to the session's backend and replaces the
//! result with its first lines and a path the model can read back with
//! `read_file`. Three kinds of result are covered: a string, a structured
//! value (written as pretty-printed JSON, so `read_file` can page it by
//! line) and an error (its message is offloaded; the code and the details
//! stay).
//!
//! Durability: the replacement is the call's result from then on, so the
//! tool-end event, the session log and every later provider request hold
//! the pointer, never the payload. The backend write finishes before the
//! hook answers, so a logged pointer always names a written file (for a
//! durable backend). A crash after the write and before the tool end is
//! durable leaves an unreferenced file; the resumed run reports the call as
//! interrupted, and a retry of the same call overwrites the same path. With
//! [`super::MemorySessions`] the files live only as long as the process, so
//! a run resumed elsewhere finds the pointer but not the file.

use std::collections::BTreeSet;
use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{PostToolUse, PostToolUseRequest, PostToolUseResponse};
use linch_core::{
    CallbackError, FailMode, Hook, JsonObject, JsonValue, ToolOutput, ToolOutputError,
    to_canonical_string,
};

use super::{SessionFiles, VfsError, normalize_path};

/// Policy id of [`ResultOffload`] in the run contract.
const POLICY_ID: &str = "linch.vfs.offload";

/// Bytes per estimated token, as in the compaction estimate.
const BYTES_PER_TOKEN: u64 = 4;

/// Most bytes of the preview, whatever the line count.
const PREVIEW_MAX_BYTES: usize = 2000;

/// When and where [`ResultOffload`] moves tool results.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OffloadConfig {
    /// Results estimated above this many tokens (four bytes per token of
    /// the text, or of the compact JSON of a structured value) are
    /// offloaded.
    pub threshold_tokens: u64,
    /// Leading lines kept inline as a preview (default 10).
    pub preview_lines: usize,
    /// Virtual directory offloaded results are written under (default
    /// `/offload`).
    pub path_prefix: String,
    /// Tools whose results are never offloaded. The default holds the four
    /// virtual filesystem tools, so reading an offloaded file back is never
    /// offloaded again.
    pub skip_tools: BTreeSet<String>,
}

impl OffloadConfig {
    /// Offload above `threshold_tokens`, with every other field at its
    /// default.
    pub fn new(threshold_tokens: u64) -> Self {
        let skip_tools = ["ls", "read_file", "write_file", "edit_file"];
        Self {
            threshold_tokens,
            preview_lines: 10,
            path_prefix: "/offload".to_owned(),
            skip_tools: skip_tools.into_iter().map(str::to_owned).collect(),
        }
    }
}

/// A `post_tool_use` hook that offloads oversized results to the calling
/// session's backend.
///
/// A string result, a structured value and an error message are all
/// candidates. A structured value is stored as pretty-printed JSON (one
/// value per line, so a model can page it) and its metadata says
/// `original_kind: "json"`; an error keeps its code and its details, which
/// gain the sizes and the path. Results under the threshold are not
/// covered: they add up over many calls, which compaction handles. A
/// failed backend write is a hook error, and the hook fails open: the
/// original result goes to the model and the failure shows in the hook
/// record.
pub struct ResultOffload {
    files: Arc<dyn SessionFiles>,
    config: OffloadConfig,
    /// The normalized [`OffloadConfig::path_prefix`].
    prefix: String,
}

impl ResultOffload {
    /// A hook that offloads by `config` into the backends of `files`.
    ///
    /// Raises:
    ///     VfsError: `InvalidPath` when the path prefix holds `..` or a NUL
    ///         byte.
    pub fn new(files: Arc<dyn SessionFiles>, config: OffloadConfig) -> Result<Self, VfsError> {
        let prefix = normalize_path(&config.path_prefix)?;
        Ok(Self {
            files,
            config,
            prefix,
        })
    }

    /// The replacement for `text` stored at `path`, which stands for a
    /// result of `tokens` tokens and `bytes` bytes.
    fn pointer(&self, text: &str, path: &str, tokens: u64, bytes: u64) -> String {
        let lines = self.config.preview_lines;
        let preview: Vec<&str> = text.split('\n').take(lines).collect();
        let mut preview = preview.join("\n");
        // One very long line (minified text) would otherwise be previewed whole.
        let cut = preview.len() > PREVIEW_MAX_BYTES;
        let mut end = PREVIEW_MAX_BYTES.min(preview.len());
        while !preview.is_char_boundary(end) {
            end -= 1;
        }
        preview.truncate(end);
        let (shown, note) = match cut {
            true => (
                preview.split('\n').count(),
                format!(" (cut at {PREVIEW_MAX_BYTES} bytes)"),
            ),
            false => (lines, String::new()),
        };
        let prefix = &self.prefix;
        format!(
            "{preview}\n\n[Tool result truncated: ~{tokens} tokens ({bytes} bytes) offloaded to \
             '{path}'. Showing the first {shown} lines{note}. Use read_file(path=\"{path}\", \
             offset=..., limit=...) to read the rest, or ls(\"{prefix}\") to list offloaded \
             results.]"
        )
    }

    /// Write `text` to the call's file and return its path.
    async fn store(
        &self,
        request: &PostToolUseRequest,
        text: &str,
    ) -> Result<String, CallbackError> {
        let name = format!("{}_{}.txt", request.tool_name, request.tool_use_id);
        let path = normalize_path(&format!("{}/{name}", self.prefix))?;
        let backend = self.files.backend(&request.session_id);
        backend.write(&path, text).await?;
        Ok(path)
    }

    /// Offload a successful result, when its value is long.
    async fn offload_output(
        &self,
        request: &PostToolUseRequest,
        output: &ToolOutput,
    ) -> Result<PostToolUseResponse, CallbackError> {
        // A string is stored as it is; any other value as pretty JSON, sized
        // by the compact JSON the model would have been shown (a value too
        // deep to encode is left to the scheduler's own error).
        let (bytes, structured) = match &output.value {
            JsonValue::String(text) => (text.len() as u64, false),
            value => match to_canonical_string(value) {
                Ok(compact) => (compact.len() as u64, true),
                Err(_) => return Ok(PostToolUseResponse::Continue),
            },
        };
        let tokens = bytes / BYTES_PER_TOKEN;
        if tokens <= self.config.threshold_tokens {
            return Ok(PostToolUseResponse::Continue);
        }
        let text = match &output.value {
            JsonValue::String(text) => text.clone(),
            value => serde_json::to_string_pretty(value)?,
        };
        let path = self.store(request, &text).await?;
        let mut metadata = output.metadata.clone();
        sizes(&mut metadata, &path, tokens, bytes);
        if structured {
            metadata.insert("original_kind".into(), "json".into());
        }
        let value = JsonValue::String(self.pointer(&text, &path, tokens, bytes));
        Ok(PostToolUseResponse::Replace {
            result: Ok(ToolOutput { value, metadata }),
        })
    }

    /// Offload the message of a failed result, when it is long. The code
    /// stays, and so do the details, which gain the sizes and the path
    /// when they are an object or absent.
    async fn offload_error(
        &self,
        request: &PostToolUseRequest,
        error: &ToolOutputError,
    ) -> Result<PostToolUseResponse, CallbackError> {
        let bytes = error.message.len() as u64;
        let tokens = bytes / BYTES_PER_TOKEN;
        if tokens <= self.config.threshold_tokens {
            return Ok(PostToolUseResponse::Continue);
        }
        let path = self.store(request, &error.message).await?;
        let details = match &error.details {
            None => Some(JsonObject::new()),
            Some(JsonValue::Object(details)) => Some(details.clone()),
            // Details that are not an object never reach the event.
            Some(_) => None,
        };
        let details = match details {
            Some(mut details) => {
                sizes(&mut details, &path, tokens, bytes);
                Some(JsonValue::Object(details))
            }
            None => error.details.clone(),
        };
        Ok(PostToolUseResponse::Replace {
            result: Err(ToolOutputError {
                message: self.pointer(&error.message, &path, tokens, bytes),
                code: error.code.clone(),
                details,
            }),
        })
    }
}

/// The host-facing facts of an offloaded result: where it went and how big
/// it was, never the payload.
fn sizes(into: &mut JsonObject, path: &str, tokens: u64, bytes: u64) {
    into.insert("offloaded_to".into(), path.into());
    into.insert("original_tokens".into(), tokens.into());
    into.insert("original_bytes".into(), bytes.into());
}

#[async_trait]
impl Hook<PostToolUse> for ResultOffload {
    fn policy_id(&self) -> &str {
        POLICY_ID
    }

    /// Raised when the hook came to cover a structured value, an error and
    /// one very long line: the policy is in the run contract, so a stored
    /// run of an agent that offloads does not resume across that change.
    fn policy_version(&self) -> u32 {
        2
    }

    /// Always open: a storage failure must never withhold a result.
    fn fail_mode(&self) -> Option<FailMode> {
        Some(FailMode::Open)
    }

    async fn call(
        &self,
        request: &PostToolUseRequest,
    ) -> Result<PostToolUseResponse, CallbackError> {
        if self.config.skip_tools.contains(&request.tool_name) {
            return Ok(PostToolUseResponse::Continue);
        }
        match &request.result {
            Ok(output) => self.offload_output(request, output).await,
            Err(error) => self.offload_error(request, error).await,
        }
    }
}
