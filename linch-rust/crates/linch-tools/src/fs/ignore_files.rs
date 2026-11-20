//! The ignore files a search honours, read from the workspace root.
//!
//! The `ignore` crate opens `.ignore` and `.gitignore` by path and follows a
//! symlink there, so a workspace file linking to a host file would make an
//! answer depend on that file's content. The search turns the crate's own
//! reading off and reads the files here, through the [`Opener`] (no symlink
//! on the way or at the end, regular files only, at most [`MAX_FILE`] bytes;
//! any other is skipped without a word), with the precedence of the crate's
//! walk: an override glob beats every ignore file; else the nearest `.ignore`
//! rule that matches wins, then the nearest `.gitignore` rule; a name no rule
//! matches is dropped only if it is hidden and hidden names are not wanted.
//! The directories from the workspace root down to the entry are consulted,
//! also those above the searched path. `.git/info/exclude` and the host's
//! global ignore file are not read.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};

use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::overrides::Override;
use ignore::{DirEntry, WalkBuilder};

use super::FsError;
use super::beneath::Opener;
use super::local::is_hidden;

/// Largest ignore file that is read, in bytes.
const MAX_FILE: u64 = 1 << 20;

/// The rules of one directory.
#[derive(Default)]
struct Dir {
    ignore: Option<Gitignore>,
    git: Option<Gitignore>,
}

struct State {
    opener: Opener,
    root: PathBuf,
    overrides: Override,
    hide_hidden: bool,
    dirs: HashMap<PathBuf, Dir>,
    /// The first error that is not "cannot be read from the root": the
    /// process is out of descriptors.
    error: Option<FsError>,
}

/// A handle on the ignore rules of one search; clones share them.
#[derive(Clone)]
pub(super) struct IgnoreFiles(Arc<Mutex<State>>);

impl IgnoreFiles {
    pub(super) fn new(opener: Opener, root: &Path, overrides: Override, hide_hidden: bool) -> Self {
        Self(Arc::new(Mutex::new(State {
            opener,
            root: root.to_owned(),
            overrides,
            hide_hidden,
            dirs: HashMap::new(),
            error: None,
        })))
    }

    /// Make `walk` list through these rules instead of reading ignore files
    /// itself.
    pub(super) fn install(&self, walk: &mut WalkBuilder) {
        walk.hidden(false)
            .ignore(false)
            .git_ignore(false)
            .git_global(false)
            .git_exclude(false)
            .parents(false);
        let rules = self.clone();
        walk.filter_entry(move |entry| rules.keeps(entry));
    }

    /// The error that stopped the rules from being read, if any. An answer
    /// made without them would be wrong, so a search gives this instead.
    pub(super) fn check(&self) -> Result<(), FsError> {
        match self.state().error.take() {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn keeps(&self, entry: &DirEntry) -> bool {
        let is_dir = entry.file_type().is_some_and(|kind| kind.is_dir());
        self.state().keeps(entry.path(), is_dir)
    }
}

impl State {
    fn keeps(&mut self, path: &Path, is_dir: bool) -> bool {
        if self.error.is_some() {
            return false;
        }
        if !self.overrides.matched(path, is_dir).is_none() {
            return true;
        }
        for dir in self.dirs_above(path) {
            self.load(&dir);
        }
        let verdict = self
            .verdict(path, is_dir, |dir| dir.ignore.as_ref())
            .or_else(|| self.verdict(path, is_dir, |dir| dir.git.as_ref()));
        match verdict {
            Some(ignored) => !ignored,
            None => !(self.hide_hidden && path.file_name().is_some_and(is_hidden)),
        }
    }

    /// The directories of the workspace that hold `path`, nearest first.
    fn dirs_above(&self, path: &Path) -> Vec<PathBuf> {
        (path.ancestors().skip(1))
            .take_while(|dir| dir.starts_with(&self.root))
            .map(Path::to_owned)
            .collect()
    }

    /// What the nearest matching rule of one kind says: `Some(true)` to
    /// ignore, `Some(false)` for a negation.
    fn verdict<F>(&self, path: &Path, is_dir: bool, pick: F) -> Option<bool>
    where
        F: Fn(&Dir) -> Option<&Gitignore>,
    {
        path.ancestors()
            .skip(1)
            .take_while(|dir| dir.starts_with(&self.root))
            .filter_map(|dir| pick(self.dirs.get(dir)?))
            .map(|rules| rules.matched(path, is_dir))
            .find(|found| !found.is_none())
            .map(|found| found.is_ignore())
    }

    fn load(&mut self, dir: &Path) {
        if self.dirs.contains_key(dir) {
            return;
        }
        let rules = Dir {
            ignore: self.read(dir, ".ignore"),
            git: self.read(dir, ".gitignore"),
        };
        self.dirs.insert(dir.to_owned(), rules);
    }

    fn read(&mut self, dir: &Path, name: &str) -> Option<Gitignore> {
        let file = match self.opener.open(&dir.join(name)) {
            Ok(file) => file?,
            Err(error) => {
                self.error.get_or_insert(error);
                return None;
            }
        };
        let mut bytes = Vec::new();
        file.take(MAX_FILE + 1).read_to_end(&mut bytes).ok()?;
        if bytes.len() as u64 > MAX_FILE {
            return None;
        }
        let text = String::from_utf8_lossy(&bytes);
        let mut builder = GitignoreBuilder::new(dir);
        for line in text.trim_start_matches('\u{feff}').lines() {
            // A line that is not a valid pattern is skipped, as the crate does.
            let _ = builder.add_line(None, line);
        }
        builder.build().ok()
    }
}
