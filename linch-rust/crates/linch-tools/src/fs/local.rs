//! [`LocalFs`]: the execution world's files on this host, confined to a root.

use std::ffi::OsStr;
use std::fs::{self, File, Metadata, Permissions};
use std::io::{self, Read as _, Write as _};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};

use async_trait::async_trait;
use tokio_util::sync::CancellationToken;

use super::search;
use super::{
    ExecutionFs, FileContent, FileInfo, FileStamp, FsError, GlobHit, GlobQuery, GrepHits, GrepQuery,
};

/// Files on this host under one root directory.
///
/// Every path is resolved with symlinks followed and must stay under the
/// root (W-B decision 5); `..`, absolute paths and symlinks that lead out
/// are refused with [`FsError::Escape`]. The check guards path arguments,
/// not a concurrent process swapping symlinks between the check and the
/// use. Blocking file work runs on tokio's blocking pool.
#[derive(Debug, Clone)]
pub struct LocalFs {
    root: PathBuf,
}

impl LocalFs {
    /// Confine to `root`, which must be an existing directory. The root is
    /// canonicalized once here.
    ///
    /// Blocking: call it before the runtime starts or from a blocking task.
    pub fn new(root: impl AsRef<Path>) -> Result<Self, FsError> {
        let given = root.as_ref();
        let shown = given.display().to_string();
        let root = fs::canonicalize(given).map_err(|error| io_error(&shown, error))?;
        if !root.is_dir() {
            return Err(FsError::NotADirectory { path: shown });
        }
        Ok(Self { root })
    }

    /// The canonical root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The real path `path` names, checked to be under the root.
    ///
    /// The longest existing prefix is canonicalized; the rest may only hold
    /// plain names, so it cannot contain a symlink or climb with `..`.
    pub(crate) fn resolve(&self, path: &str) -> Result<PathBuf, FsError> {
        let escape = || FsError::Escape {
            path: path.to_owned(),
        };
        let full = self.root.join(path);
        let parts: Vec<Component<'_>> = full.components().collect();
        let mut split = parts.len();
        let base = loop {
            let prefix: PathBuf = parts[..split].iter().collect();
            match fs::canonicalize(&prefix) {
                Ok(real) => break real,
                Err(error) if error.kind() == io::ErrorKind::NotFound && split > 1 => split -= 1,
                Err(error) if error.kind() == io::ErrorKind::NotADirectory => {
                    return Err(FsError::NotADirectory {
                        path: path.to_owned(),
                    });
                }
                Err(error) => return Err(io_error(path, error)),
            }
        };
        let mut real = base;
        for (index, part) in parts[split..].iter().enumerate() {
            let Component::Normal(name) = part else {
                return Err(escape());
            };
            real.push(name);
            // The first missing name is either absent or a dangling symlink;
            // writing through a dangling symlink would create its target.
            if index == 0 && fs::symlink_metadata(&real).is_ok() {
                return Err(escape());
            }
        }
        if real.starts_with(&self.root) {
            Ok(real)
        } else {
            Err(escape())
        }
    }

    /// Workspace path of a real path under the root.
    pub(crate) fn workspace_path(&self, real: &Path) -> String {
        let relative = real.strip_prefix(&self.root).unwrap_or(real);
        let names: Vec<&str> = relative
            .components()
            .filter_map(|part| match part {
                Component::Normal(name) => Some(name.to_str().unwrap_or("\u{fffd}")),
                _ => None,
            })
            .collect();
        if names.is_empty() {
            ".".to_owned()
        } else {
            names.join("/")
        }
    }

    fn stat_blocking(&self, path: &str) -> Result<FileInfo, FsError> {
        let real = self.resolve(path)?;
        let shown = self.workspace_path(&real);
        let stamp = match fs::metadata(&real) {
            Ok(meta) if meta.is_file() => Some(stamp_of(&meta)?),
            Ok(_) => return Err(FsError::NotAFile { path: shown }),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(io_error(&shown, error)),
        };
        Ok(FileInfo { path: shown, stamp })
    }

    fn read_blocking(&self, path: &str, max_bytes: u64) -> Result<FileContent, FsError> {
        let real = self.resolve(path)?;
        let shown = self.workspace_path(&real);
        // Checked before opening: opening a FIFO would block.
        let meta = fs::metadata(&real).map_err(|error| io_error(path, error))?;
        if !meta.is_file() {
            return Err(FsError::NotAFile { path: shown });
        }
        let too_large = || FsError::TooLarge {
            path: shown.clone(),
            limit: max_bytes,
        };
        let file = File::open(&real).map_err(|error| io_error(&shown, error))?;
        let open = file.metadata().map_err(|error| io_error(&shown, error))?;
        if open.len() > max_bytes {
            return Err(too_large());
        }
        let stamp = stamp_of(&open)?;
        let mut bytes = Vec::new();
        file.take(max_bytes.saturating_add(1))
            .read_to_end(&mut bytes)
            .map_err(|error| io_error(&shown, error))?;
        if bytes.len() as u64 > max_bytes {
            return Err(too_large());
        }
        let after = fs::metadata(&real).map_err(|error| io_error(&shown, error))?;
        if stamp_of(&after)? != stamp || bytes.len() as u64 != stamp.len {
            return Err(FsError::Changed { path: shown });
        }
        Ok(FileContent {
            path: shown,
            bytes,
            stamp,
        })
    }

    fn write_blocking(
        &self,
        path: &str,
        content: &[u8],
        expect: Option<FileStamp>,
    ) -> Result<FileStamp, FsError> {
        let real = self.resolve(path)?;
        let shown = self.workspace_path(&real);
        let fail = |error: io::Error| io_error(&shown, error);
        let (Some(parent), Some(_)) = (real.parent(), real.file_name()) else {
            return Err(FsError::NotAFile { path: shown });
        };
        if real == self.root {
            return Err(FsError::NotAFile { path: shown });
        }
        let current = match fs::metadata(&real) {
            Ok(meta) if meta.is_file() => Some(meta),
            Ok(_) => return Err(FsError::NotAFile { path: shown }),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(fail(error)),
        };
        if let Some(expected) = expect {
            let now = current.as_ref().map(stamp_of).transpose()?;
            if now != Some(expected) {
                return Err(FsError::Changed { path: shown });
            }
        }
        fs::create_dir_all(parent).map_err(fail)?;
        let mode = current.map_or_else(|| Permissions::from_mode(0o666), |meta| meta.permissions());
        let mut temp = tempfile::Builder::new()
            .prefix(".linch-")
            .suffix(".tmp")
            .permissions(mode.clone())
            .tempfile_in(parent)
            .map_err(fail)?;
        temp.write_all(content).map_err(fail)?;
        // `permissions` above is masked by the umask; an existing file keeps
        // its exact mode.
        temp.as_file().set_permissions(mode).map_err(fail)?;
        temp.persist(&real).map_err(|error| fail(error.error))?;
        let meta = fs::metadata(&real).map_err(fail)?;
        stamp_of(&meta)
    }

    async fn blocking<T: Send + 'static>(
        &self,
        work: impl FnOnce(Self) -> Result<T, FsError> + Send + 'static,
    ) -> Result<T, FsError> {
        let fs = self.clone();
        match tokio::task::spawn_blocking(move || work(fs)).await {
            Ok(result) => result,
            Err(error) if error.is_panic() => std::panic::resume_unwind(error.into_panic()),
            Err(_) => Err(FsError::Cancelled),
        }
    }
}

#[async_trait]
impl ExecutionFs for LocalFs {
    async fn stat(&self, path: &str) -> Result<FileInfo, FsError> {
        let path = path.to_owned();
        self.blocking(move |fs| fs.stat_blocking(&path)).await
    }

    async fn read(&self, path: &str, max_bytes: u64) -> Result<FileContent, FsError> {
        let path = path.to_owned();
        self.blocking(move |fs| fs.read_blocking(&path, max_bytes))
            .await
    }

    async fn write(
        &self,
        path: &str,
        content: Vec<u8>,
        expect: Option<FileStamp>,
    ) -> Result<FileStamp, FsError> {
        let path = path.to_owned();
        self.blocking(move |fs| fs.write_blocking(&path, &content, expect))
            .await
    }

    async fn grep(
        &self,
        query: &GrepQuery,
        cancel: &CancellationToken,
    ) -> Result<GrepHits, FsError> {
        let query = query.clone();
        // Dropping this future (a call timeout) stops the walk too.
        let stop = cancel.child_token();
        let _guard = stop.clone().drop_guard();
        self.blocking(move |fs| search::grep(&fs, &query, &stop))
            .await
    }

    async fn glob(
        &self,
        query: &GlobQuery,
        cancel: &CancellationToken,
    ) -> Result<Vec<GlobHit>, FsError> {
        let query = query.clone();
        let stop = cancel.child_token();
        let _guard = stop.clone().drop_guard();
        self.blocking(move |fs| search::glob(&fs, &query, &stop))
            .await
    }
}

/// The stamp of a file's metadata.
pub(super) fn stamp_of(meta: &Metadata) -> Result<FileStamp, FsError> {
    let modified = meta.modified().map_err(|error| FsError::Io {
        path: String::new(),
        source: error,
    })?;
    Ok(FileStamp {
        modified,
        len: meta.len(),
    })
}

/// `error` on `path`, with a missing file reported as [`FsError::NotFound`].
pub(super) fn io_error(path: &str, error: io::Error) -> FsError {
    if error.kind() == io::ErrorKind::NotFound {
        FsError::NotFound {
            path: path.to_owned(),
        }
    } else {
        FsError::Io {
            path: path.to_owned(),
            source: error,
        }
    }
}

/// Whether a name starts with a dot.
pub(super) fn is_hidden(name: &OsStr) -> bool {
    name.as_encoded_bytes().first() == Some(&b'.')
}
