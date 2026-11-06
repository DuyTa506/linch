//! The execution world's file layer: one [`ExecutionFs`] trait that every file
//! tool goes through, and [`LocalFs`], a disk implementation confined to a
//! root directory.
//!
//! Paths given to the trait are what the model wrote: relative to the root,
//! or absolute under it. Paths returned are *workspace paths*: relative to
//! the root, `/`-separated, with every symlink resolved, so one file always
//! has one name. The read-before-write policy keys on these names.

mod local;
mod search;
#[cfg(test)]
mod tests;

pub use local::LocalFs;

use std::io;
use std::time::SystemTime;

use async_trait::async_trait;
use thiserror::Error;
use tokio_util::sync::CancellationToken;

/// When a file last changed. Two equal stamps mean the content is unchanged.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FileStamp {
    /// Last modification time.
    pub modified: SystemTime,
    /// Size in bytes.
    pub len: u64,
}

/// A resolved path and, when a regular file exists there, its stamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileInfo {
    /// Workspace path of the target.
    pub path: String,
    /// Stamp of the file, or `None` when nothing exists at the path yet.
    pub stamp: Option<FileStamp>,
}

/// A whole file read in one go.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileContent {
    /// Workspace path of the file.
    pub path: String,
    /// The file's bytes.
    pub bytes: Vec<u8>,
    /// Stamp taken while the file was open; the read is consistent with it.
    pub stamp: FileStamp,
}

/// What a [`GrepQuery`] reports per file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrepMode {
    /// Only the paths of files that match.
    FilesWithMatches,
    /// The number of matching lines per file.
    Count,
    /// Matching lines, with optional context lines.
    Content,
}

/// A regex search over files under the root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrepQuery {
    /// Regex in Rust `regex` syntax.
    pub pattern: String,
    /// File or directory to search; the root when `None`.
    pub path: Option<String>,
    /// File filter with ripgrep `--glob` rules, relative to the search path.
    pub glob: Option<String>,
    /// File type from ripgrep's type table (`rust`, `py`, `ts`, ...).
    pub file_type: Option<String>,
    /// What to report.
    pub mode: GrepMode,
    /// Whether matching ignores case.
    pub case_insensitive: bool,
    /// Whether a match may span lines (`.` then matches a newline too).
    pub multiline: bool,
    /// Context lines before each match ([`GrepMode::Content`] only).
    pub before: usize,
    /// Context lines after each match ([`GrepMode::Content`] only).
    pub after: usize,
    /// Most entries to return: files in the file modes, lines in
    /// [`GrepMode::Content`]. The search stops once it has one more.
    pub limit: usize,
}

/// One reported line of a [`GrepMode::Content`] search.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrepLine {
    /// 1-based line number.
    pub number: u64,
    /// Line text without its line terminator, decoded lossily.
    pub text: String,
    /// Whether the line matched (`false` for a context line).
    pub is_match: bool,
}

/// One file with at least one match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrepFile {
    /// Workspace path of the file.
    pub path: String,
    /// Matching lines counted ([`GrepMode::Count`] only, else 0).
    pub count: u64,
    /// Reported lines ([`GrepMode::Content`] only), in file order.
    pub lines: Vec<GrepLine>,
}

/// The result of a [`GrepQuery`].
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GrepHits {
    /// Matching files, sorted by path.
    pub files: Vec<GrepFile>,
    /// Whether the search stopped at [`GrepQuery::limit`].
    pub truncated: bool,
}

/// A file-name search under the root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GlobQuery {
    /// Glob with ripgrep `--glob` rules, relative to the search directory.
    pub pattern: String,
    /// Directory to search; the root when `None`.
    pub path: Option<String>,
}

/// One file found by a [`GlobQuery`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GlobHit {
    /// Workspace path of the file.
    pub path: String,
    /// Last modification time, when it could be read.
    pub modified: Option<SystemTime>,
}

/// Why a file operation failed. The message is what the model sees.
#[derive(Debug, Error)]
pub enum FsError {
    /// The path resolves outside the root, directly, through `..` or
    /// through a symlink.
    #[error("{path}: path escapes the workspace root")]
    Escape {
        /// The path as given.
        path: String,
    },
    /// Nothing exists at the path.
    #[error("{path}: no such file")]
    NotFound {
        /// The path as given.
        path: String,
    },
    /// The path names a directory or another non-regular file.
    #[error("{path}: not a regular file")]
    NotAFile {
        /// Workspace path of the target.
        path: String,
    },
    /// The path names something other than a directory.
    #[error("{path}: not a directory")]
    NotADirectory {
        /// Workspace path of the target.
        path: String,
    },
    /// The file is larger than the read limit.
    #[error("{path}: file exceeds {limit} bytes; use Grep to narrow the search")]
    TooLarge {
        /// Workspace path of the file.
        path: String,
        /// The limit in bytes.
        limit: u64,
    },
    /// The file changed while it was read, or before a guarded write.
    #[error("{path}: file changed on disk; Read it again")]
    Changed {
        /// Workspace path of the file.
        path: String,
    },
    /// A regex, glob or file type in a query is invalid.
    #[error("invalid query: {0}")]
    InvalidQuery(String),
    /// The search was cancelled.
    #[error("search cancelled")]
    Cancelled,
    /// Any other I/O failure.
    #[error("{path}: {source}")]
    Io {
        /// The path involved.
        path: String,
        /// The underlying error.
        #[source]
        source: io::Error,
    },
}

/// The file half of an execution world.
///
/// The workspace tools use only this trait, so a remote execution world can
/// replace [`LocalFs`] without changing them. Implementations confine every
/// path to their root, resolving symlinks before checking.
#[async_trait]
pub trait ExecutionFs: Send + Sync {
    /// Resolve `path` and stat it.
    ///
    /// Returns [`FsError::NotAFile`] when something other than a regular
    /// file is there, and a `None` stamp when nothing is.
    async fn stat(&self, path: &str) -> Result<FileInfo, FsError>;

    /// Read the whole regular file at `path`, refusing files over
    /// `max_bytes` and files that change while being read.
    async fn read(&self, path: &str, max_bytes: u64) -> Result<FileContent, FsError>;

    /// Replace the file at `path` with `content` atomically, creating parent
    /// directories, and return the new stamp.
    ///
    /// With `expect`, the write fails with [`FsError::Changed`] unless the
    /// file currently has that stamp.
    async fn write(
        &self,
        path: &str,
        content: Vec<u8>,
        expect: Option<FileStamp>,
    ) -> Result<FileStamp, FsError>;

    /// Search file contents. Hidden and ignored files are skipped, symlinks
    /// are not followed, and binary files are skipped. Returns
    /// [`FsError::Cancelled`] soon after `cancel` fires.
    async fn grep(
        &self,
        query: &GrepQuery,
        cancel: &CancellationToken,
    ) -> Result<GrepHits, FsError>;

    /// List files whose names match. Ignored files are skipped, hidden ones
    /// too unless the pattern names a dot segment, and symlinks are not
    /// followed. Returns [`FsError::Cancelled`] soon after `cancel` fires.
    async fn glob(
        &self,
        query: &GlobQuery,
        cancel: &CancellationToken,
    ) -> Result<Vec<GlobHit>, FsError>;
}
