//! [`ScopedBackend`]: one subtree of another backend, as its own root.

use std::sync::Arc;

use async_trait::async_trait;

use super::{FileBackend, VfsError, file_path, is_under, normalize_path};

/// The subtree under `root` of a shared backend, with paths relative to it.
///
/// `/notes.txt` here is `<root>/notes.txt` in the inner backend. Nothing
/// outside the root can be named (`..` is refused by [`normalize_path`]),
/// listings show only files under the root, and errors name the scoped
/// path, not the inner one. The inner backend is shared, not owned: many
/// scopes can sit on one store, for example one per tenant.
pub struct ScopedBackend {
    inner: Arc<dyn FileBackend>,
    root: String,
}

impl ScopedBackend {
    /// The subtree of `inner` under `root`.
    ///
    /// Raises:
    ///     VfsError: `InvalidPath` when `root` holds `..` or a NUL byte.
    pub fn new(inner: Arc<dyn FileBackend>, root: &str) -> Result<Self, VfsError> {
        Ok(Self {
            inner,
            root: normalize_path(root)?,
        })
    }

    /// The inner path of normalized scoped `path`.
    fn inner_path(&self, path: &str) -> String {
        match (self.root.as_str(), path) {
            ("/", path) => path.to_owned(),
            (root, "/") => root.to_owned(),
            (root, path) => format!("{root}{path}"),
        }
    }

    /// The scoped path of inner `path`, or `None` when it lies outside the
    /// root or is the root itself.
    fn scoped_path(&self, path: &str) -> Option<String> {
        if self.root == "/" {
            return Some(path.to_owned());
        }
        let rest = path.strip_prefix(&self.root)?;
        rest.starts_with('/').then(|| rest.to_owned())
    }

    /// The scoped file path and its inner path.
    fn file(&self, path: &str) -> Result<(String, String), VfsError> {
        let scoped = file_path(path)?;
        let inner = self.inner_path(&scoped);
        Ok((scoped, inner))
    }
}

/// `error` with its inner path replaced by the scoped one.
fn rename(error: VfsError, scoped: &str) -> VfsError {
    let path = scoped.to_owned();
    match error {
        VfsError::NotFound { .. } => VfsError::NotFound { path },
        VfsError::NoMatch { .. } => VfsError::NoMatch { path },
        VfsError::NotUnique { count, .. } => VfsError::NotUnique { path, count },
        VfsError::InvalidPath { reason, .. } => VfsError::InvalidPath { path, reason },
        other => other,
    }
}

#[async_trait]
impl FileBackend for ScopedBackend {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        let (scoped, inner) = self.file(path)?;
        self.inner
            .read(&inner)
            .await
            .map_err(|e| rename(e, &scoped))
    }

    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        let (scoped, inner) = self.file(path)?;
        (self.inner.write(&inner, content).await).map_err(|e| rename(e, &scoped))
    }

    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError> {
        let (scoped, inner) = self.file(path)?;
        (self.inner.edit(&inner, old, new, replace_all).await).map_err(|e| rename(e, &scoped))
    }

    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        let prefix = normalize_path(prefix)?;
        let inner = self.inner_path(&prefix);
        let listed = self.inner.ls(&inner).await?;
        // Filtered again, in case the inner backend lists more than asked.
        let mut scoped: Vec<String> = listed
            .iter()
            .filter(|path| is_under(path, &inner))
            .filter_map(|path| self.scoped_path(path))
            .collect();
        scoped.sort();
        scoped.dedup();
        Ok(scoped)
    }

    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        let (scoped, inner) = self.file(path)?;
        self.inner
            .exists(&inner)
            .await
            .map_err(|e| rename(e, &scoped))
    }

    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        let (scoped, inner) = self.file(path)?;
        self.inner
            .delete(&inner)
            .await
            .map_err(|e| rename(e, &scoped))
    }
}
