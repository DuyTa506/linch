//! [`CompositeBackend`]: route paths to backends by longest prefix.

use std::collections::BTreeSet;
use std::sync::Arc;

use async_trait::async_trait;

use super::{FileBackend, VfsError, file_path, is_under, normalize_path};

/// Paths routed to backends by their longest matching prefix, everything
/// else to a default.
///
/// The common setup keeps bulky scratch in memory and notes in a durable
/// store:
///
/// ```
/// # use std::sync::Arc;
/// # use linch_tools::vfs::{CompositeBackend, MemoryBackend};
/// # fn durable() -> Arc<MemoryBackend> { Arc::new(MemoryBackend::new()) }
/// let files = CompositeBackend::new(Arc::new(MemoryBackend::new()))
///     .with_route("/memories", durable())?;
/// # Ok::<(), linch_tools::vfs::VfsError>(())
/// ```
///
/// A route matches by whole segments, so `/memories` takes `/memories/a`
/// but not `/memoriesx`. Routed backends see the full path. `ls` merges the
/// listings of every backend.
pub struct CompositeBackend {
    default: Arc<dyn FileBackend>,
    /// Normalized prefixes and their backends.
    routes: Vec<(String, Arc<dyn FileBackend>)>,
}

impl CompositeBackend {
    /// Every path goes to `default` until routes are added.
    pub fn new(default: Arc<dyn FileBackend>) -> Self {
        Self {
            default,
            routes: Vec::new(),
        }
    }

    /// This composite with paths at or under `prefix` sent to `backend`. A
    /// second route for the same prefix replaces the first.
    ///
    /// Raises:
    ///     VfsError: `InvalidPath` when `prefix` holds `..` or a NUL byte.
    pub fn with_route(
        mut self,
        prefix: &str,
        backend: Arc<dyn FileBackend>,
    ) -> Result<Self, VfsError> {
        let prefix = normalize_path(prefix)?;
        self.routes.retain(|(known, _)| *known != prefix);
        self.routes.push((prefix, backend));
        Ok(self)
    }

    /// The backend for normalized `path`: the longest route that holds it.
    fn route(&self, path: &str) -> &dyn FileBackend {
        self.routes
            .iter()
            .filter(|(prefix, _)| is_under(path, prefix))
            .max_by_key(|(prefix, _)| prefix.len())
            .map_or(&*self.default, |(_, backend)| &**backend)
    }

    /// The normalized file path and its backend.
    fn file(&self, path: &str) -> Result<(String, &dyn FileBackend), VfsError> {
        let path = file_path(path)?;
        let backend = self.route(&path);
        Ok((path, backend))
    }
}

#[async_trait]
impl FileBackend for CompositeBackend {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        let (path, backend) = self.file(path)?;
        backend.read(&path).await
    }

    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        let (path, backend) = self.file(path)?;
        backend.write(&path, content).await
    }

    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError> {
        let (path, backend) = self.file(path)?;
        backend.edit(&path, old, new, replace_all).await
    }

    /// Every backend's listing, merged. A backend that fails fails the
    /// listing.
    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        let prefix = normalize_path(prefix)?;
        let mut paths = BTreeSet::new();
        let backends = std::iter::once(&self.default).chain(self.routes.iter().map(|(_, b)| b));
        for backend in backends {
            paths.extend(backend.ls(&prefix).await?);
        }
        Ok(paths.into_iter().collect())
    }

    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        let (path, backend) = self.file(path)?;
        backend.exists(&path).await
    }

    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        let (path, backend) = self.file(path)?;
        backend.delete(&path).await
    }
}
