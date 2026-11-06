//! [`FileMemoryContext`]: shows the model its file memory index, the
//! first lines of `/memories/MEMORY.md` in the virtual filesystem, with
//! every request (Python's `FileMemoryContextBuilder`).

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{ContextBuild, ContextBuildRequest, ContextBuildResponse};
use linch_core::vfs::{FileBackend, VfsError};
use linch_core::{CallbackError, Hook, SessionId, TextBlock};

/// Path of the file memory index: one line per memory file.
pub const MEMORY_INDEX: &str = "/memories/MEMORY.md";

/// Most lines of the index shown to the model.
pub const MEMORY_INDEX_LINES: usize = 200;

/// The text shown before the index, as in Python.
pub const MEMORY_INSTRUCTION: &str = "Memories live under /memories/, one file per fact. \
     MEMORY.md is a one-line-per-file index. Use the file tools to read and maintain \
     memories; update or delete stale facts.";

/// Stable id of the hook's policy, in the run contract (Python's
/// `resume_policy_id`).
const POLICY_ID: &str = "linch.deep-agent.file-memory-context";

/// Finds the backend that holds a session's files.
type Files = Arc<dyn Fn(&SessionId) -> Arc<dyn FileBackend> + Send + Sync>;

/// A `context_build` hook that adds [`MEMORY_INSTRUCTION`] to every
/// request and, when the session's files hold a non-empty
/// [`MEMORY_INDEX`], its first [`MEMORY_INDEX_LINES`] lines (with a note
/// when it is longer).
///
/// The blocks join the tail of the request only: they are never logged,
/// and the cached prefix stays byte-identical. The index is read again
/// for each request, so a change the model made shows on its next turn.
/// Nothing is written. A failed read is a hook error, which the chain
/// skips (the point fails open).
pub struct FileMemoryContext {
    files: Files,
}

impl FileMemoryContext {
    /// A hook that reads the index of a session from the backend `files`
    /// gives for it: the one the virtual filesystem tools of that session
    /// use (`linch_tools::vfs::SessionFiles::backend`).
    pub fn new(files: impl Fn(&SessionId) -> Arc<dyn FileBackend> + Send + Sync + 'static) -> Self {
        Self {
            files: Arc::new(files),
        }
    }
}

#[async_trait]
impl Hook<ContextBuild> for FileMemoryContext {
    fn policy_id(&self) -> &str {
        POLICY_ID
    }

    fn policy_version(&self) -> u32 {
        1
    }

    async fn call(
        &self,
        request: &ContextBuildRequest,
    ) -> Result<ContextBuildResponse, CallbackError> {
        let backend = (self.files)(&request.session_id);
        let index = match backend.read(MEMORY_INDEX).await {
            Ok(index) => index,
            Err(VfsError::NotFound { .. }) => String::new(),
            Err(error) => return Err(Box::new(error)),
        };
        let text = MEMORY_INSTRUCTION.to_owned();
        let mut blocks = vec![TextBlock { text }];
        let shown = head(&index);
        if !shown.is_empty() {
            let text = format!("{MEMORY_INDEX}:\n{shown}");
            blocks.push(TextBlock { text });
        }
        Ok(ContextBuildResponse { blocks })
    }
}

/// The first [`MEMORY_INDEX_LINES`] lines of `index`, with a note when it
/// has more.
fn head(index: &str) -> String {
    let lines: Vec<&str> = index.lines().collect();
    let mut shown = lines[..lines.len().min(MEMORY_INDEX_LINES)].join("\n");
    if lines.len() > MEMORY_INDEX_LINES {
        shown.push_str("\n[MEMORY.md truncated to 200 lines.]");
    }
    shown
}
