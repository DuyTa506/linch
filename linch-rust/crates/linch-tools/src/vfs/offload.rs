//! [`ResultOffload`]: move oversized tool results into the virtual
//! filesystem, as a `post_tool_use` hook.
//!
//! When a tool's text result is estimated above
//! [`OffloadConfig::threshold_tokens`], the hook writes the whole text to
//! the session's backend and replaces the result with its first lines and a
//! path the model can read back with `read_file`.
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
use linch_core::{CallbackError, FailMode, Hook, JsonValue, ToolOutput};

use super::{SessionFiles, VfsError, normalize_path};

/// Policy id of [`ResultOffload`] in the run contract.
const POLICY_ID: &str = "linch.vfs.offload";

/// Bytes per estimated token, as in the compaction estimate.
const BYTES_PER_TOKEN: u64 = 4;

/// When and where [`ResultOffload`] moves tool results.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OffloadConfig {
    /// Text results estimated above this many tokens (four bytes per token)
    /// are offloaded.
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

/// A `post_tool_use` hook that offloads oversized text results to the
/// calling session's backend.
///
/// Only successful results whose value is a string are candidates; errors
/// and structured values pass through. A failed backend write is a hook
/// error, and the hook fails open: the original result goes to the model
/// and the failure shows in the hook record.
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

    /// The replacement for `text` stored at `path`.
    fn pointer(&self, text: &str, path: &str, tokens: u64) -> String {
        let lines = self.config.preview_lines;
        let preview: Vec<&str> = text.split('\n').take(lines).collect();
        let (preview, bytes, prefix) = (preview.join("\n"), text.len(), &self.prefix);
        format!(
            "{preview}\n\n[Tool result truncated: ~{tokens} tokens ({bytes} bytes) offloaded to \
             '{path}'. Showing the first {lines} lines. Use read_file(path=\"{path}\", \
             offset=..., limit=...) to read the rest, or ls(\"{prefix}\") to list offloaded \
             results.]"
        )
    }
}

#[async_trait]
impl Hook<PostToolUse> for ResultOffload {
    fn policy_id(&self) -> &str {
        POLICY_ID
    }

    fn policy_version(&self) -> u32 {
        1
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
        let Ok(output) = &request.result else {
            return Ok(PostToolUseResponse::Continue);
        };
        let JsonValue::String(text) = &output.value else {
            return Ok(PostToolUseResponse::Continue);
        };
        let tokens = text.len() as u64 / BYTES_PER_TOKEN;
        if tokens <= self.config.threshold_tokens {
            return Ok(PostToolUseResponse::Continue);
        }
        let name = format!("{}_{}.txt", request.tool_name, request.tool_use_id);
        let path = normalize_path(&format!("{}/{name}", self.prefix))?;
        let backend = self.files.backend(&request.session_id);
        backend.write(&path, text).await?;
        let mut metadata = output.metadata.clone();
        metadata.insert("offloaded_to".into(), path.clone().into());
        metadata.insert("original_tokens".into(), tokens.into());
        metadata.insert("original_bytes".into(), (text.len() as u64).into());
        let value = JsonValue::String(self.pointer(text, &path, tokens));
        Ok(PostToolUseResponse::Replace {
            result: Ok(ToolOutput { value, metadata }),
        })
    }
}
