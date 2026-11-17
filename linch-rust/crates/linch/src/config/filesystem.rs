//! [`FilesystemConfig`]: the virtual filesystem of an agent.

use linch_ext::deep_agent::MemoryScope;
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
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesystemConfig {
    /// Move a text tool result estimated above this many tokens (four bytes
    /// per token) into the filesystem, leaving its first lines and its path
    /// in the result (see [`linch_tools::vfs::ResultOffload`]). Absent,
    /// with no `offload_threshold_fraction` either: results are never
    /// offloaded. Turning offload on or off changes the run contract, so a
    /// run cannot resume across that change; changing the threshold does
    /// not.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offload_threshold_tokens: Option<u64>,
    /// Offload above this fraction of the provider's context window for
    /// the spec's model, at least 1000 tokens (Python's
    /// `OffloadConfig.threshold_fraction`, 0.1 there). Used only when
    /// `offload_threshold_tokens` is absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offload_threshold_fraction: Option<f64>,
    /// Show the model its file memory: the instruction to keep memories under
    /// `/memories/`, and the first 200 lines (at most 16 KiB) of its
    /// `MEMORY.md`, fenced as untrusted data, when the session's files hold
    /// it (Python's `FileMemoryContextBuilder`; see
    /// [`linch_ext::deep_agent::FileMemoryContext`]). Both ride the tail of
    /// each request, never the log, and the file is read again for each
    /// request, so a change the model made shows on its next turn. Nothing
    /// is written for the model: it keeps the file with the filesystem
    /// tools. A deep agent always has this. Off when absent, which keeps it
    /// off the wire; turning it on adds the hook to the run contract. With
    /// memory stores the file lives and dies with its session, so it does
    /// not outlive [`Agent::close_session`](linch_core::Agent::close_session);
    /// with Postgres stores every session of the namespace shares the
    /// files, and [`Self::memory_scope`] says whose `MEMORY.md` it is.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub memory: bool,
    /// Whose `/memories/` the model sees: `principal` (the default when
    /// absent) gives each run's principal a directory of its own,
    /// `/memories/<key>/MEMORY.md`, so a run with fewer rights cannot plant
    /// notes another principal's runs read; a run without a principal shares
    /// `/memories/MEMORY.md`. `namespace` is that one file for every run
    /// of the namespace. Used only when `memory` is on or the agent is a deep
    /// agent; absent keeps it off the wire. The scope is part of the run
    /// contract.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_scope: Option<MemoryScope>,
}

/// The least threshold a fraction gives (Python's floor).
const MIN_FRACTION_THRESHOLD: u64 = 1_000;

impl FilesystemConfig {
    /// The offload threshold in tokens for a model whose context window is
    /// `window`: `offload_threshold_tokens`, else the fraction of the window
    /// (at least 1000); `None` when offload is off.
    pub fn offload_threshold(&self, window: u64) -> Option<u64> {
        self.offload_threshold_tokens.or_else(|| {
            let fraction = self.offload_threshold_fraction?;
            Some(((window as f64 * fraction) as u64).max(MIN_FRACTION_THRESHOLD))
        })
    }
}
