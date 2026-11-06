//! [`FilesystemConfig`]: the virtual filesystem of an agent.

use serde::{Deserialize, Serialize};

/// Turns on the virtual filesystem of [`linch_tools::vfs`]: the `ls`,
/// `read_file`, `write_file` and `edit_file` tools over text files that are
/// not the workspace, and [`linch_tools::vfs::PROMPT`] after the system
/// prompt, which teaches the model to use them.
///
/// Where the files live follows the stores. With memory stores each
/// session has its own files in memory, dropped by
/// [`Agent::close_session`](linch_core::Agent::close_session). With
/// Postgres stores every session of the namespace shares the files in
/// `linch_files`, so a run resumed in another process still finds them.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemConfig {
    /// Move a text tool result estimated above this many tokens (four bytes
    /// per token) into the filesystem, leaving its first lines and its path
    /// in the result (see [`linch_tools::vfs::ResultOffload`]). Absent:
    /// results are never offloaded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offload_threshold_tokens: Option<u64>,
}
