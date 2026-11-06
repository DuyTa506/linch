//! A virtual filesystem for scratch notes and offloaded tool results.
//!
//! A [`FileBackend`] is a small async store of UTF-8 text files keyed by
//! `/`-rooted paths. It is not the workspace: [`crate::Read`] and the other
//! workspace tools work on real files under the workspace root, while these
//! files live wherever the backend keeps them:
//!
//! - [`MemoryBackend`]: in memory, gone with the process.
//! - [`DiskBackend`]: real files under a root directory, confined like
//!   [`crate::LocalFs`].
//! - `linch_postgres::PgFileBackend`: a Postgres table, shared across
//!   processes.
//! - [`CompositeBackend`] routes paths by longest prefix to other backends,
//!   and [`ScopedBackend`] exposes one subtree of another backend.
//!
//! The `ls`, `read_file`, `write_file` and `edit_file` tools
//! ([`vfs_tools`]) and the [`ResultOffload`] hook, which moves oversized
//! tool results into the filesystem, find a session's backend through
//! [`SessionFiles`]:
//! [`MemorySessions`] gives each session a fresh [`MemoryBackend`], and
//! [`SharedFiles`] gives every session one backend.
//!
//! Every backend applies [`normalize_path`] to the paths it is given, so
//! `a//b/` and `/a/b` name one file, and `..` or a NUL byte is refused.
//!
//! The [`FileBackend`] contract, its errors, the path helpers and the
//! [`conformance`] suite live in `linch_core::vfs`, so a store crate can
//! implement the trait without these tools; they are re-exported here.

mod composite;
mod disk;
mod memory;
mod offload;
mod scoped;
mod tools;

use std::sync::Arc;

use linch_core::SessionId;

pub use composite::CompositeBackend;
pub use disk::DiskBackend;
pub use linch_core::vfs::{
    BackendError, FileBackend, VfsError, apply_edit, conformance, file_path, normalize_path,
};
pub use memory::{MemoryBackend, MemorySessions};
pub use offload::{OffloadConfig, ResultOffload};
pub use scoped::ScopedBackend;
pub use tools::{EditFile, Ls, PROMPT, ReadFile, WriteFile, vfs_tools};

/// Finds the backend that holds one session's files, for the tools and
/// the offload hook.
pub trait SessionFiles: Send + Sync {
    /// The backend of `session_id`.
    fn backend(&self, session_id: &SessionId) -> Arc<dyn FileBackend>;
}

/// One backend for every session: files written in one session are seen
/// by all of them (Python's explicit `filesystem=` backend).
pub struct SharedFiles {
    backend: Arc<dyn FileBackend>,
}

impl SharedFiles {
    /// Every session uses `backend`.
    pub fn new(backend: Arc<dyn FileBackend>) -> Self {
        Self { backend }
    }
}

impl SessionFiles for SharedFiles {
    fn backend(&self, _session_id: &SessionId) -> Arc<dyn FileBackend> {
        Arc::clone(&self.backend)
    }
}

/// Whether normalized `path` is normalized `prefix` or lies under it.
pub(crate) fn is_under(path: &str, prefix: &str) -> bool {
    prefix == "/"
        || path
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}
