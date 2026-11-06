//! The virtual filesystem contract: [`FileBackend`], a small async store of
//! UTF-8 text files keyed by `/`-rooted paths, for scratch notes and
//! offloaded tool results.
//!
//! The trait lives in the core so a store crate (`linch_postgres`) can
//! implement it without depending on the tools. The backends, the file
//! tools and the result offload hook are in `linch_tools::vfs`, which
//! re-exports everything here.
//!
//! Every backend applies [`normalize_path`] to the paths it is given, so
//! `a//b/` and `/a/b` name one file, and `..` or a NUL byte is refused.
//! Every backend passes [`conformance::backend_suite`].

pub mod conformance;

use async_trait::async_trait;
use thiserror::Error;

/// Boxed error of a storage layer under a backend.
pub type BackendError = Box<dyn std::error::Error + Send + Sync>;

/// Why a virtual file operation failed.
#[derive(Debug, Error)]
pub enum VfsError {
    /// The path holds `..` or a NUL byte, names the root as a file, or
    /// leads out of a disk backend's root.
    #[error("invalid path {path:?}: {reason}")]
    InvalidPath {
        /// The path as given.
        path: String,
        /// What is wrong with it.
        reason: &'static str,
    },
    /// No file exists at the path.
    #[error("{path}: no such file")]
    NotFound {
        /// The normalized path.
        path: String,
    },
    /// An edit's old text does not occur in the file.
    #[error("old string not found in {path}")]
    NoMatch {
        /// The normalized path.
        path: String,
    },
    /// An edit's old text occurs more than once and `replace_all` is off.
    #[error(
        "old string is not unique in {path} ({count} matches); \
         pass replace_all=true or include more context"
    )]
    NotUnique {
        /// The normalized path.
        path: String,
        /// How many times the old text occurs.
        count: usize,
    },
    /// The storage under the backend failed.
    #[error("file backend failed: {0}")]
    Backend(#[source] BackendError),
}

/// An async store of UTF-8 text files keyed by `/`-rooted paths.
///
/// Implementations normalize every path with [`normalize_path`] and refuse
/// the root `/` as a file. `ls` lists files only, sorted by bytes. `edit` is
/// atomic per backend: no write to the same file lands between its read and
/// its write.
#[async_trait]
pub trait FileBackend: Send + Sync {
    /// The whole content of the file at `path`.
    ///
    /// Raises [`VfsError::NotFound`] when no file is there.
    async fn read(&self, path: &str) -> Result<String, VfsError>;

    /// Create or replace the file at `path` with `content`.
    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError>;

    /// Replace `old` with `new` in the file at `path`, as [`apply_edit`]
    /// does, and return the number of replacements.
    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError>;

    /// The sorted paths of the files at or under `prefix`, matched by whole
    /// segments (`/a` holds `/a/x`, not `/ab`). An empty prefix or `/` lists
    /// every file.
    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError>;

    /// Whether a file exists at `path`.
    async fn exists(&self, path: &str) -> Result<bool, VfsError>;

    /// Remove the file at `path`. Removing a missing file does nothing.
    async fn delete(&self, path: &str) -> Result<(), VfsError>;
}

/// `path` in canonical form: one leading `/`, no empty or `.` segments, no
/// trailing `/`. `""` and `/` become `/`.
///
/// Raises:
///     VfsError: `InvalidPath` when a segment is `..` or the path holds a
///         NUL byte.
pub fn normalize_path(path: &str) -> Result<String, VfsError> {
    if path.contains('\0') {
        return Err(invalid(path, "contains a NUL byte"));
    }
    let mut normal = String::with_capacity(path.len() + 1);
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => return Err(invalid(path, "may not contain '..'")),
            name => {
                normal.push('/');
                normal.push_str(name);
            }
        }
    }
    if normal.is_empty() {
        normal.push('/');
    }
    Ok(normal)
}

/// `text` with `old` replaced by `new`, and the number of replacements:
/// exactly one occurrence, or every occurrence with `replace_all`.
///
/// Each backend calls this inside its own atomic read and write. An empty
/// `old` matches nowhere.
///
/// Args:
///     path: Normalized path of the file, for the error.
///     text: Current content of the file.
///     old: Exact text to find.
///     new: Text that replaces each match.
///     replace_all: Whether to replace every occurrence instead of requiring
///         exactly one.
///
/// Returns:
///     The new content and how many matches were replaced.
///
/// Raises:
///     VfsError: `NoMatch` when `old` does not occur, `NotUnique` when it
///         occurs more than once without `replace_all`.
pub fn apply_edit(
    path: &str,
    text: &str,
    old: &str,
    new: &str,
    replace_all: bool,
) -> Result<(String, usize), VfsError> {
    let count = if old.is_empty() {
        0
    } else {
        text.matches(old).count()
    };
    match count {
        0 => Err(VfsError::NoMatch {
            path: path.to_owned(),
        }),
        1 => Ok((text.replacen(old, new, 1), 1)),
        count if !replace_all => Err(VfsError::NotUnique {
            path: path.to_owned(),
            count,
        }),
        count => Ok((text.replace(old, new), count)),
    }
}

/// `path` normalized by [`normalize_path`] and checked to name a file, not
/// the root. Backends apply it to every file argument.
///
/// Raises:
///     VfsError: `InvalidPath` when [`normalize_path`] refuses `path` or it
///         names the root.
pub fn file_path(path: &str) -> Result<String, VfsError> {
    let normal = normalize_path(path)?;
    if normal == "/" {
        return Err(invalid(path, "the root is a directory, not a file"));
    }
    Ok(normal)
}

fn invalid(path: &str, reason: &'static str) -> VfsError {
    VfsError::InvalidPath {
        path: path.to_owned(),
        reason,
    }
}
