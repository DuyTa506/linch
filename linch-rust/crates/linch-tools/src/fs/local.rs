//! [`LocalFs`]: the execution world's files on this host, confined to a root.

use std::borrow::Cow;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{self, Read as _, Write as _};
use std::os::fd::OwnedFd;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use async_trait::async_trait;
use rustix::fs::{
    AtFlags, Mode, OFlags, fchmod, fstat, mkdirat, open, openat, renameat, statat, unlinkat,
};
use rustix::io::Errno;
use tokio_util::sync::CancellationToken;

use super::beneath::{self, Links, Located, Opener, is_file, open_file, stamp_of_stat};
use super::search;
use super::{
    ExecutionFs, FileContent, FileInfo, FileStamp, FsError, GlobHits, GlobQuery, GrepHits,
    GrepQuery,
};
use crate::read::MAX_READ_BYTES;

/// Files on this host under one root directory.
///
/// The root is opened once and every path is walked from that descriptor,
/// one name at a time, never following a symlink out of the root. `..`,
/// absolute paths outside the root and symlinks that lead out are refused
/// with [`FsError::Escape`]. A symlink inside the root whose target stays
/// inside it is followed, an absolute target included if it spells the
/// canonical root. A concurrent process that swaps a directory or a file for
/// a symlink between two steps of a call cannot move the call out of the
/// root: the swapped name is refused where it is next opened.
///
/// Not covered: hard links (a second name for a file outside the root is a
/// file inside it), mounts inside the root, and a process that moves the
/// root directory itself (the descriptor stays on the directory it opened).
/// Blocking file work runs on tokio's blocking pool.
#[derive(Debug, Clone)]
pub struct LocalFs {
    root: PathBuf,
    /// The root as it was given to [`Self::new`], made absolute, when that
    /// differs from the canonical root (the root was reached through a
    /// symlink).
    spelled: Option<PathBuf>,
    /// The root directory, opened once; every walk starts here.
    root_fd: Arc<OwnedFd>,
    /// Most bytes of one file a search holds in memory.
    search_limit: u64,
}

impl LocalFs {
    /// Confine to `root`, which must be an existing directory. The root is
    /// canonicalized and opened once here.
    ///
    /// Blocking: call it before the runtime starts or from a blocking task.
    pub fn new(root: impl AsRef<Path>) -> Result<Self, FsError> {
        let given = root.as_ref();
        let shown = given.display().to_string();
        let root = fs::canonicalize(given).map_err(|error| io_error(&shown, error))?;
        if !root.is_dir() {
            return Err(FsError::NotADirectory { path: shown });
        }
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let root_fd =
            open(&root, flags, Mode::empty()).map_err(|error| io_error(&shown, error.into()))?;
        let spelled = std::path::absolute(given)
            .ok()
            .filter(|spelled| *spelled != root);
        Ok(Self {
            root,
            spelled,
            root_fd: Arc::new(root_fd),
            search_limit: MAX_READ_BYTES,
        })
    }

    /// This root with searches limited to `bytes` per file, so a test can
    /// trip the limit without a big file.
    #[cfg(test)]
    pub(crate) fn with_search_limit(mut self, bytes: u64) -> Self {
        self.search_limit = bytes;
        self
    }

    /// Most bytes of one file a search holds in memory.
    pub(crate) fn search_limit(&self) -> u64 {
        self.search_limit
    }

    /// The canonical root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Where `path` leads, walked from the root descriptor.
    ///
    /// The deepest directory that exists is held open in the result and the
    /// rest may only hold plain names, so a missing part cannot contain a
    /// symlink or climb with `..`.
    pub(super) fn locate(&self, path: &str) -> Result<Located, FsError> {
        let relative = self.relative(Path::new(path), path)?;
        beneath::locate(&self.root_fd, &self.root, &relative, path, Links::Follow)
    }

    /// Every regular file under the root, as real paths. Blocking.
    pub(crate) fn files(&self) -> Result<Vec<PathBuf>, FsError> {
        search::files(self)
    }

    /// Opens the files a search found by name, through the root descriptor.
    pub(crate) fn opener(&self) -> Opener {
        Opener::new(&self.root_fd, &self.root)
    }

    /// `given` as a path below the root. An absolute path is below it only
    /// if it starts with the canonical root or with the root as it was given
    /// to [`Self::new`]; it is refused otherwise, whether or not anything
    /// exists there, so the answer says nothing about the host. The walk that
    /// follows starts at the root descriptor, so this only translates a name.
    fn relative<'a>(&self, given: &'a Path, shown: &str) -> Result<Cow<'a, Path>, FsError> {
        if !given.is_absolute() {
            return Ok(Cow::Borrowed(given));
        }
        let inside = given.strip_prefix(&self.root).ok().or_else(|| {
            let spelled = self.spelled.as_deref()?;
            given.strip_prefix(spelled).ok()
        });
        inside.map(Cow::Borrowed).ok_or_else(|| escape(shown))
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
        let at = self.locate(path)?;
        let shown = at.workspace_path();
        let stamp = match &at.leaf {
            _ if at.tail.is_empty() => return Err(FsError::NotAFile { path: shown }),
            Some(stat) if is_file(stat) => Some(stamp_of_stat(stat)),
            Some(_) => return Err(FsError::NotAFile { path: shown }),
            None => None,
        };
        Ok(FileInfo { path: shown, stamp })
    }

    fn read_blocking(&self, path: &str, max_bytes: u64) -> Result<FileContent, FsError> {
        let at = self.locate(path)?;
        let shown = at.workspace_path();
        let missing = || FsError::NotFound {
            path: path.to_owned(),
        };
        let not_a_file = || FsError::NotAFile {
            path: shown.clone(),
        };
        let too_large = || FsError::TooLarge {
            path: shown.clone(),
            limit: max_bytes,
        };
        if at.tail.is_empty() {
            return Err(not_a_file());
        }
        let (Some(name), Some(leaf)) = (at.entry(), &at.leaf) else {
            return Err(missing());
        };
        // Checked before opening; the open does not block on a FIFO either.
        if !is_file(leaf) {
            return Err(not_a_file());
        }
        let file = open_file(&at.dir, name, &shown)?;
        let open = fstat(&file).map_err(|error| io_error(&shown, error.into()))?;
        if !is_file(&open) {
            return Err(not_a_file());
        }
        let stamp = stamp_of_stat(&open);
        if stamp.len > max_bytes {
            return Err(too_large());
        }
        let mut bytes = Vec::new();
        file.take(max_bytes.saturating_add(1))
            .read_to_end(&mut bytes)
            .map_err(|error| io_error(&shown, error))?;
        if bytes.len() as u64 > max_bytes {
            return Err(too_large());
        }
        let after = statat(&*at.dir, name, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| io_error(&shown, error.into()))?;
        if stamp_of_stat(&after) != stamp || bytes.len() as u64 != stamp.len {
            return Err(FsError::ChangedWhileReading { path: shown });
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
        let at = self.locate(path)?;
        let shown = at.workspace_path();
        let fail = |error: io::Error| io_error(&shown, error);
        let Some((name, parents)) = at.tail.split_last() else {
            return Err(FsError::NotAFile { path: shown });
        };
        let current = match &at.leaf {
            Some(stat) if is_file(stat) => Some(*stat),
            Some(_) => return Err(FsError::NotAFile { path: shown }),
            None => None,
        };
        if let Some(expected) = expect
            && current.as_ref().map(stamp_of_stat) != Some(expected)
        {
            return Err(FsError::Changed { path: shown });
        }
        let mut dir = Arc::clone(&at.dir);
        for part in parents {
            dir = Arc::new(make_dir(&dir, part, &shown)?);
        }
        let (temp_name, mut temp) = create_temp(&dir).map_err(fail)?;
        let written = (|| -> io::Result<()> {
            temp.write_all(content)?;
            if let Some(stat) = &current {
                // An existing file keeps its exact mode (not masked).
                fchmod(&temp, Mode::from_raw_mode(stat.st_mode))?;
            }
            Ok(renameat(&*dir, &temp_name, &*dir, name.as_os_str())?)
        })();
        if let Err(error) = written {
            let _ = unlinkat(&*dir, &temp_name, AtFlags::empty());
            return Err(fail(error));
        }
        let stat = statat(&*dir, name.as_os_str(), AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|error| fail(error.into()))?;
        Ok(stamp_of_stat(&stat))
    }

    /// Remove the regular file at `path`; a missing file or a directory is
    /// left alone.
    pub(crate) fn remove_file_blocking(&self, path: &str) -> Result<(), FsError> {
        let at = self.locate(path)?;
        let (Some(name), Some(leaf)) = (at.entry(), &at.leaf) else {
            return Ok(());
        };
        if !is_file(leaf) {
            return Ok(());
        }
        match unlinkat(&*at.dir, name, AtFlags::empty()) {
            Ok(()) | Err(Errno::NOENT) => Ok(()),
            Err(error) => Err(io_error(&at.workspace_path(), error.into())),
        }
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

    /// Run [`Self::remove_file_blocking`] on the blocking pool.
    pub(crate) async fn remove_file(&self, path: &str) -> Result<(), FsError> {
        let path = path.to_owned();
        self.blocking(move |fs| fs.remove_file_blocking(&path))
            .await
    }
}

/// Create the directory `name` in `dir` if it is missing and open it.
fn make_dir(dir: &Arc<OwnedFd>, name: &OsStr, shown: &str) -> Result<OwnedFd, FsError> {
    match mkdirat(&**dir, name, Mode::from_raw_mode(0o777)) {
        Ok(()) | Err(Errno::EXIST) => {}
        Err(error) => return Err(io_error(shown, error.into())),
    }
    beneath::open_dir(dir, name).map_err(|error| match error {
        Errno::LOOP => escape(shown),
        Errno::NOTDIR => FsError::NotADirectory {
            path: shown.to_owned(),
        },
        other => io_error(shown, other.into()),
    })
}

/// A new temporary file beside its target, mode 0666 masked by the umask
/// like any created file. The name is unique by `O_EXCL`, not by luck.
fn create_temp(dir: &OwnedFd) -> io::Result<(OsString, File)> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let flags = OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    for _ in 0..16 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |since| since.subsec_nanos());
        let name = format!(
            ".linch-{:x}{:x}{:x}.tmp",
            std::process::id(),
            nanos,
            COUNTER.fetch_add(1, Ordering::Relaxed)
        );
        match openat(dir, &name, flags, Mode::from_raw_mode(0o666)) {
            Ok(fd) => return Ok((name.into(), File::from(fd))),
            Err(Errno::EXIST) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Err(io::ErrorKind::AlreadyExists.into())
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
    ) -> Result<GlobHits, FsError> {
        let query = query.clone();
        let stop = cancel.child_token();
        let _guard = stop.clone().drop_guard();
        self.blocking(move |fs| search::glob(&fs, &query, &stop))
            .await
    }
}

fn escape(path: &str) -> FsError {
    FsError::Escape {
        path: path.to_owned(),
    }
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
