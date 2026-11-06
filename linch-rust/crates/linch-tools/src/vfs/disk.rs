//! [`DiskBackend`]: virtual files as real files under one root directory.

use std::fs;
use std::io;
use std::path::{Component, Path};

use async_trait::async_trait;
use tokio::sync::Mutex;

use super::{FileBackend, VfsError, apply_edit, file_path, is_under, normalize_path};
use crate::fs::{ExecutionFs, FsError, LocalFs};

/// Virtual files stored as real files under a root directory, for offloaded
/// results a person can open, grep or keep.
///
/// Paths are confined like the workspace tools' ([`LocalFs`]): symlinks are
/// resolved and a path that leads out of the root is
/// [`VfsError::InvalidPath`]; `ls` does not follow symlinks. Writes are
/// atomic (a temp file beside the target, then a rename). Writes, edits and
/// deletes on one backend run one at a time, and an edit fails if another
/// process changed the file between its read and its write. File work runs
/// on tokio's blocking pool.
#[derive(Debug)]
pub struct DiskBackend {
    fs: LocalFs,
    writes: Mutex<()>,
}

impl DiskBackend {
    /// Files under `root`, which is created if missing and canonicalized.
    ///
    /// Blocking: call it before the runtime starts or from a blocking task.
    ///
    /// Raises:
    ///     VfsError: `Backend` when the root cannot be created or is not a
    ///         directory.
    pub fn new(root: impl AsRef<Path>) -> Result<Self, VfsError> {
        let root = root.as_ref();
        fs::create_dir_all(root).map_err(|error| VfsError::Backend(error.into()))?;
        let fs = LocalFs::new(root).map_err(|error| VfsError::Backend(error.into()))?;
        Ok(Self {
            fs,
            writes: Mutex::new(()),
        })
    }

    /// The canonical root directory.
    pub fn root(&self) -> &Path {
        self.fs.root()
    }

    async fn blocking<T: Send + 'static>(
        &self,
        work: impl FnOnce(LocalFs) -> Result<T, VfsError> + Send + 'static,
    ) -> Result<T, VfsError> {
        let fs = self.fs.clone();
        match tokio::task::spawn_blocking(move || work(fs)).await {
            Ok(result) => result,
            Err(error) if error.is_panic() => std::panic::resume_unwind(error.into_panic()),
            Err(error) => Err(VfsError::Backend(error.into())),
        }
    }
}

/// The normalized file path and the same path relative to the root.
fn paths(path: &str) -> Result<(String, String), VfsError> {
    let normal = file_path(path)?;
    let relative = normal[1..].to_owned();
    Ok((normal, relative))
}

/// `error` from [`LocalFs`] on the virtual file `path`.
fn fs_error(path: &str, error: FsError) -> VfsError {
    match error {
        FsError::Escape { .. } => VfsError::InvalidPath {
            path: path.to_owned(),
            reason: "leads out of the backend root",
        },
        FsError::NotFound { .. } | FsError::NotAFile { .. } | FsError::NotADirectory { .. } => {
            VfsError::NotFound {
                path: path.to_owned(),
            }
        }
        other => VfsError::Backend(other.into()),
    }
}

/// `error` from a write, where a directory in the way is a failure, not a
/// missing file.
fn write_error(path: &str, error: FsError) -> VfsError {
    match error {
        FsError::Escape { .. } => fs_error(path, error),
        other => VfsError::Backend(other.into()),
    }
}

fn text(path: &str, bytes: Vec<u8>) -> Result<String, VfsError> {
    String::from_utf8(bytes).map_err(|_| {
        let message = format!("{path} is not UTF-8 text");
        VfsError::Backend(io::Error::new(io::ErrorKind::InvalidData, message).into())
    })
}

#[async_trait]
impl FileBackend for DiskBackend {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        let (normal, relative) = paths(path)?;
        let file = (self.fs.read(&relative, u64::MAX).await).map_err(|e| fs_error(&normal, e))?;
        text(&normal, file.bytes)
    }

    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        let (normal, relative) = paths(path)?;
        let _writing = self.writes.lock().await;
        let bytes = content.as_bytes().to_vec();
        (self.fs.write(&relative, bytes, None).await).map_err(|e| write_error(&normal, e))?;
        Ok(())
    }

    async fn edit(
        &self,
        path: &str,
        old: &str,
        new: &str,
        replace_all: bool,
    ) -> Result<usize, VfsError> {
        let (normal, relative) = paths(path)?;
        let _writing = self.writes.lock().await;
        let file = (self.fs.read(&relative, u64::MAX).await).map_err(|e| fs_error(&normal, e))?;
        let current = text(&normal, file.bytes)?;
        let (updated, count) = apply_edit(&normal, &current, old, new, replace_all)?;
        let bytes = updated.into_bytes();
        // Fails with `Changed` if another process wrote the file meanwhile.
        (self.fs.write(&relative, bytes, Some(file.stamp)).await)
            .map_err(|e| write_error(&normal, e))?;
        Ok(count)
    }

    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        let prefix = normalize_path(prefix)?;
        let mut found = self.blocking(|fs| Ok(walk(fs.root()))).await?;
        found.retain(|path| is_under(path, &prefix));
        found.sort();
        Ok(found)
    }

    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        let (normal, relative) = paths(path)?;
        match self.fs.stat(&relative).await {
            Ok(info) => Ok(info.stamp.is_some()),
            Err(error) => match fs_error(&normal, error) {
                VfsError::NotFound { .. } => Ok(false),
                other => Err(other),
            },
        }
    }

    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        let (normal, relative) = paths(path)?;
        let _writing = self.writes.lock().await;
        self.blocking(move |fs| {
            let real = match fs.resolve(&relative) {
                Ok(real) => real,
                Err(error) => {
                    return match fs_error(&normal, error) {
                        VfsError::NotFound { .. } => Ok(()),
                        other => Err(other),
                    };
                }
            };
            match fs::metadata(&real) {
                Ok(meta) if meta.is_file() => {}
                Ok(_) => return Ok(()),
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(VfsError::Backend(error.into())),
            }
            match fs::remove_file(&real) {
                Err(error) if error.kind() != io::ErrorKind::NotFound => {
                    Err(VfsError::Backend(error.into()))
                }
                _ => Ok(()),
            }
        })
        .await
    }
}

/// Virtual paths of the regular files under `root`. Symlinks are not
/// followed, and names that are not UTF-8 are left out (no virtual path can
/// name them).
fn walk(root: &Path) -> Vec<String> {
    let mut walk = ignore::WalkBuilder::new(root);
    walk.standard_filters(false).follow_links(false);
    walk.build()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_some_and(|kind| kind.is_file()))
        .filter_map(|entry| virtual_path(root, entry.path()))
        .collect()
}

fn virtual_path(root: &Path, real: &Path) -> Option<String> {
    let mut path = String::new();
    for part in real.strip_prefix(root).ok()?.components() {
        let Component::Normal(name) = part else {
            return None;
        };
        path.push('/');
        path.push_str(name.to_str()?);
    }
    Some(path)
}
