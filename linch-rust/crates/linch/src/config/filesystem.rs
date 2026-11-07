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
