//! Path resolution by capability: every name is opened relative to a
//! directory descriptor, one component at a time, so no step can leave the
//! root however a concurrent process rearranges the tree.
//!
//! A path is never handed to the kernel whole. Each directory on it is opened
//! with `openat(dir, name, O_NOFOLLOW)`; a symlink is read and its target is
//! spliced into the remaining names; `..` pops the directory the walk is in
//! (never the kernel's `..`, which follows a moved directory out of the
//! root). The directory descriptor in hand is always one this walk
//! opened beneath the root, so a name swapped for a symlink afterwards is
//! refused by `O_NOFOLLOW` instead of followed. Only POSIX `*at` calls are
//! used, so the same code runs on Linux and macOS.

use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io;
use std::os::fd::OwnedFd;
use std::os::unix::ffi::OsStringExt as _;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use rustix::fs::{AtFlags, FileType, Mode, OFlags, Stat, openat, readlinkat, statat};
use rustix::io::Errno;

use super::FsError;
use super::local::io_error;

/// Most symlinks one path may pass through, as the Linux kernel allows.
const MAX_LINKS: u32 = 40;

/// Most directory levels one path may go down. Each level holds a descriptor
/// while the walk lasts, so this bounds what one call can take from the
/// process.
const MAX_DEPTH: usize = 128;

/// How a walk treats symlinks.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Links {
    /// Follow a symlink whose target stays under the root.
    Follow,
    /// Refuse any symlink on the path.
    Refuse,
}

/// Where a walk ended: the deepest directory that exists on the path, held
/// open, and the names that follow it.
#[derive(Debug)]
pub(super) struct Located {
    /// The directory, opened beneath the root.
    pub(super) dir: Arc<OwnedFd>,
    /// Names from the root to `dir`, with symlinks resolved.
    pub(super) names: Vec<OsString>,
    /// Names after `dir`. Empty when the path is `dir` itself; otherwise the
    /// first names an entry of `dir` that exists or not, and a second name
    /// means the first is missing.
    pub(super) tail: Vec<OsString>,
    /// What the first name of `tail` is, when it exists. It is never a
    /// directory or a symlink: those are walked into.
    pub(super) leaf: Option<Stat>,
}

impl Located {
    /// Workspace path: names from the root, `/`-separated, symlinks resolved.
    pub(super) fn workspace_path(&self) -> String {
        let names: Vec<&str> = self
            .names
            .iter()
            .chain(&self.tail)
            .map(|name| name.to_str().unwrap_or("\u{fffd}"))
            .collect();
        if names.is_empty() {
            ".".to_owned()
        } else {
            names.join("/")
        }
    }

    /// The path under `root`, for walks that go by name.
    pub(super) fn real_path(&self, root: &Path) -> PathBuf {
        root.join(self.names.iter().chain(&self.tail).collect::<PathBuf>())
    }

    /// The name of the entry in `dir` when `tail` holds exactly one name.
    pub(super) fn entry(&self) -> Option<&OsStr> {
        match self.tail.as_slice() {
            [name] => Some(name),
            _ => None,
        }
    }
}

/// One name still to walk.
struct Step {
    part: Part,
    /// Whether it came from a symlink's target.
    linked: bool,
}

enum Part {
    Name(OsString),
    Up,
}

/// Queue `path`'s names so that popping yields them in order.
fn push_steps(pending: &mut Vec<Step>, path: &Path, linked: bool) -> bool {
    for part in path.components().rev() {
        let part = match part {
            Component::Normal(name) => Part::Name(name.to_owned()),
            Component::ParentDir => Part::Up,
            Component::CurDir => continue,
            Component::RootDir | Component::Prefix(_) => return false,
        };
        pending.push(Step { part, linked });
    }
    true
}

/// Open the directory `name` in `dir`; a symlink there is `ELOOP` or
/// `ENOTDIR`, never followed.
pub(super) fn open_dir(dir: &OwnedFd, name: &OsStr) -> Result<OwnedFd, Errno> {
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    openat(dir, name, flags, Mode::empty())
}

/// Flags to read a file: no symlink, and no blocking on a FIFO swapped in.
pub(super) fn read_flags() -> OFlags {
    OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC
}

/// The directories from the root to the one a walk is in, each held open so
/// that `..` is a pop and never the kernel's `..`, which would follow a
/// directory that was moved.
type Trail = Vec<(OsString, Arc<OwnedFd>)>;

/// What a name in a directory is.
enum Found {
    Dir,
    Link,
    File(Stat),
    Missing,
}

/// Walk the relative path `given` from `root_fd`, which is the directory
/// `root`.
///
/// Absolute symlink targets must start with `root`. `shown` is the path as
/// the caller gave it, for errors. A name that does not exist is not an
/// error: it is the start of [`Located::tail`], unless a symlink led to it (a
/// dangling link), which is [`FsError::Escape`] because writing there would
/// create the link's target.
pub(super) fn locate(
    root_fd: &Arc<OwnedFd>,
    root: &Path,
    given: &Path,
    shown: &str,
    links: Links,
) -> Result<Located, FsError> {
    let escape = || FsError::Escape {
        path: shown.to_owned(),
    };
    let io = |error: Errno| io_error(shown, error.into());
    let mut pending = Vec::new();
    if !push_steps(&mut pending, given, false) {
        return Err(escape());
    }
    let too_deep = || io(Errno::NAMETOOLONG);
    let mut trail: Trail = Vec::new();
    let mut followed = 0;
    while let Some(Step { part, linked }) = pending.pop() {
        let name = match part {
            Part::Up => {
                if trail.pop().is_none() {
                    return Err(escape());
                }
                continue;
            }
            Part::Name(name) => name,
        };
        let dir = Arc::clone(trail.last().map_or(root_fd, |(_, fd)| fd));
        // The last name is statted so a plain file costs one call; a
        // directory name is opened, and the open says when it is not one.
        let found = if pending.is_empty() {
            match statat(&*dir, &name, AtFlags::SYMLINK_NOFOLLOW) {
                Ok(stat) => match FileType::from_raw_mode(stat.st_mode) {
                    FileType::Directory => Found::Dir,
                    FileType::Symlink => Found::Link,
                    _ => Found::File(stat),
                },
                Err(Errno::NOENT) => Found::Missing,
                Err(error) => return Err(io(error)),
            }
        } else {
            match open_dir(&dir, &name) {
                Ok(next) => {
                    trail.push((name, Arc::new(next)));
                    if trail.len() > MAX_DEPTH {
                        return Err(too_deep());
                    }
                    continue;
                }
                Err(Errno::NOENT) => Found::Missing,
                Err(Errno::NOTDIR | Errno::LOOP) => match readlinkat(&*dir, &name, Vec::new()) {
                    Ok(_) => Found::Link,
                    Err(Errno::INVAL) => {
                        return Err(FsError::NotADirectory {
                            path: shown.to_owned(),
                        });
                    }
                    Err(Errno::NOENT) => Found::Missing,
                    Err(error) => return Err(io(error)),
                },
                Err(error) => return Err(io(error)),
            }
        };
        match found {
            Found::Dir => {
                // Opened by name, so a swap since the stat is refused.
                let next = open_dir(&dir, &name).map_err(|error| match error {
                    Errno::LOOP | Errno::NOTDIR => escape(),
                    other => io(other),
                })?;
                trail.push((name, Arc::new(next)));
                if trail.len() > MAX_DEPTH {
                    return Err(too_deep());
                }
            }
            Found::Link => {
                followed += 1;
                if links == Links::Refuse {
                    return Err(escape());
                }
                if followed > MAX_LINKS {
                    return Err(io(Errno::LOOP));
                }
                let target = match readlinkat(&*dir, &name, Vec::new()) {
                    Ok(target) => PathBuf::from(OsString::from_vec(target.into_bytes())),
                    Err(error) => return Err(io(error)),
                };
                let target = if target.is_absolute() {
                    let Ok(inside) = target.strip_prefix(root) else {
                        return Err(escape());
                    };
                    trail.clear();
                    inside.to_owned()
                } else {
                    target
                };
                if !push_steps(&mut pending, &target, true) {
                    return Err(escape());
                }
            }
            Found::File(stat) => {
                return Ok(located(dir, &trail, vec![name], Some(stat)));
            }
            Found::Missing => {
                if linked {
                    return Err(escape());
                }
                let mut tail = vec![name];
                while let Some(Step { part, .. }) = pending.pop() {
                    match part {
                        Part::Name(name) => tail.push(name),
                        Part::Up => return Err(escape()),
                    }
                }
                if trail.len() + tail.len() > MAX_DEPTH {
                    return Err(too_deep());
                }
                return Ok(located(dir, &trail, tail, None));
            }
        }
    }
    let dir = Arc::clone(trail.last().map_or(root_fd, |(_, fd)| fd));
    Ok(located(dir, &trail, Vec::new(), None))
}

fn located(dir: Arc<OwnedFd>, trail: &Trail, tail: Vec<OsString>, leaf: Option<Stat>) -> Located {
    Located {
        dir,
        names: trail.iter().map(|(name, _)| name.clone()).collect(),
        tail,
        leaf,
    }
}

/// Open the regular file `name` in `dir` for reading.
///
/// A symlink swapped in since the walk is [`FsError::Escape`] and a missing
/// name is [`FsError::NotFound`]. `shown` names the file in errors.
pub(super) fn open_file(dir: &OwnedFd, name: &OsStr, shown: &str) -> Result<File, FsError> {
    match openat(dir, name, read_flags(), Mode::empty()) {
        Ok(fd) => Ok(File::from(fd)),
        Err(Errno::LOOP) => Err(FsError::Escape {
            path: shown.to_owned(),
        }),
        Err(error) => Err(io_error(shown, error.into())),
    }
}

/// The identity of a stat that [`FileStamp`](super::FileStamp) records.
pub(super) fn stamp_of_stat(stat: &Stat) -> super::FileStamp {
    // The field types differ by platform: signed or not, 32 or 64 bits.
    #[allow(clippy::unnecessary_cast)]
    let (seconds, nanos, len) = (
        stat.st_mtime as i64,
        stat.st_mtime_nsec as u32,
        stat.st_size as u64,
    );
    let modified = match u64::try_from(seconds) {
        Ok(seconds) => std::time::UNIX_EPOCH + std::time::Duration::new(seconds, nanos),
        Err(_) => {
            std::time::UNIX_EPOCH - std::time::Duration::new(seconds.unsigned_abs(), 0)
                + std::time::Duration::new(0, nanos)
        }
    };
    super::FileStamp { modified, len }
}

/// Whether `stat` is a regular file.
pub(super) fn is_file(stat: &Stat) -> bool {
    FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile
}

/// Opens the files a walk by name found, through the root descriptor.
///
/// A path from a directory walk is only a candidate: another process may
/// have swapped a directory for a symlink since it was listed. Every file is
/// walked again from the root with no symlink allowed, so nothing is read or
/// reported from outside the root.
pub(crate) struct Opener<'a> {
    root_fd: &'a Arc<OwnedFd>,
    root: &'a Path,
    /// The last parent directory, relative to the root: a sorted walk visits
    /// the files of one directory together.
    parent: Option<(PathBuf, Arc<OwnedFd>)>,
}

impl<'a> Opener<'a> {
    pub(super) fn new(root_fd: &'a Arc<OwnedFd>, root: &'a Path) -> Self {
        Self {
            root_fd,
            root,
            parent: None,
        }
    }

    /// The directory holding `real` and the file's name.
    fn parent_of<'p>(
        &mut self,
        real: &'p Path,
    ) -> Result<Option<(Arc<OwnedFd>, &'p OsStr)>, FsError> {
        let Some(relative) = real.strip_prefix(self.root).ok() else {
            return Ok(None);
        };
        let (Some(name), Some(above)) = (relative.file_name(), relative.parent()) else {
            return Ok(None);
        };
        if let Some((cached, dir)) = &self.parent
            && cached == above
        {
            return Ok(Some((Arc::clone(dir), name)));
        }
        match locate(self.root_fd, self.root, above, "", Links::Refuse) {
            Ok(at) if at.tail.is_empty() => {
                self.parent = Some((above.to_owned(), Arc::clone(&at.dir)));
                Ok(Some((at.dir, name)))
            }
            Ok(_) => Ok(None),
            Err(FsError::Io { source, .. }) if exhausted(&source) => Err(io_error(".", source)),
            Err(_) => Ok(None),
        }
    }

    /// Open the regular file at `real`, a path under the root.
    ///
    /// `Ok(None)` means it could not be read from the root: a symlink on the
    /// way, a name that is gone, something else in its place, a directory
    /// that is not searchable. Callers count such a name and never report
    /// it, since it may have come from a directory outside the root. An error
    /// is the process running out of descriptors.
    pub(crate) fn open(&mut self, real: &Path) -> Result<Option<File>, FsError> {
        let Some((dir, name)) = self.parent_of(real)? else {
            return Ok(None);
        };
        let fd = match openat(&*dir, name, read_flags(), Mode::empty()) {
            Ok(fd) => fd,
            Err(error) => return left_out(error),
        };
        let file = File::from(fd);
        let Ok(meta) = file.metadata() else {
            return Ok(None);
        };
        Ok(meta.is_file().then_some(file))
    }

    /// The stat of the regular file at `real`, a path under the root, or
    /// `None` as for [`Self::open`].
    pub(crate) fn stat(&mut self, real: &Path) -> Result<Option<Stat>, FsError> {
        let Some((dir, name)) = self.parent_of(real)? else {
            return Ok(None);
        };
        match statat(&*dir, name, AtFlags::SYMLINK_NOFOLLOW) {
            Ok(stat) => Ok(is_file(&stat).then_some(stat)),
            Err(error) => left_out(error),
        }
    }
}

/// `Ok(None)` for an error that only means a name cannot be read from the
/// root, the error itself when the process is out of descriptors.
fn left_out<T>(error: Errno) -> Result<Option<T>, FsError> {
    let error = io::Error::from(error);
    if exhausted(&error) {
        Err(io_error(".", error))
    } else {
        Ok(None)
    }
}

/// Whether `error` is the process or the system running out of descriptors.
pub(crate) fn exhausted(error: &io::Error) -> bool {
    matches!(error.raw_os_error(), Some(code)
        if code == Errno::MFILE.raw_os_error() || code == Errno::NFILE.raw_os_error())
}
