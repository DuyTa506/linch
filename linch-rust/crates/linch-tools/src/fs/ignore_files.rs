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
//! matches is dropped only if it is hidden, hidden names are not wanted and
//! no file type the search is limited to claims it.
//! The directories from the workspace root down to the entry are consulted,
//! also those above the searched path. `.git/info/exclude` and the host's
//! global ignore file are not read.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Instant;

use ignore::gitignore::{Gitignore, GitignoreBuilder};
use ignore::overrides::Override;
use ignore::types::Types;
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
    /// The file types the search is limited to: a name of such a type is
    /// exempt from the hidden filter, as in the crate's walk.
    types: Option<Types>,
    hide_hidden: bool,
    /// When the search's time limit passes; every entry after it is dropped.
    deadline: Option<Instant>,
    expired: bool,
    /// The rules of the directories from the workspace root down to the one
    /// the walk is in.
    chain: Vec<(PathBuf, Dir)>,
    /// The first error that is not "cannot be read from the root": the
    /// process is out of descriptors.
    error: Option<FsError>,
}

/// A handle on the ignore rules of one search; clones share them.
#[derive(Clone)]
pub(super) struct IgnoreFiles(Arc<Mutex<State>>);

impl IgnoreFiles {
    pub(super) fn new(
        opener: Opener,
        root: &Path,
        overrides: Override,
        hide_hidden: bool,
        deadline: Option<Instant>,
    ) -> Self {
        Self(Arc::new(Mutex::new(State {
            opener,
            root: root.to_owned(),
            overrides,
            types: None,
            hide_hidden,
            deadline,
            expired: false,
            chain: Vec::new(),
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

    /// Limit the search to `types`.
    pub(super) fn types(&self, types: Types) {
        self.state().types = Some(types);
    }

    /// Whether the time limit passed while the walk was being filtered: the
    /// walk was cut short, so the caller reports a timeout.
    pub(super) fn expired(&self) -> bool {
        self.state().expired
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
        if self.error.is_some() || self.expired {
            return false;
        }
        if self
            .deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            self.expired = true;
            return false;
        }
        if !self.overrides.matched(path, is_dir).is_none() {
            return true;
        }
        if let Some(parent) = path.parent() {
            self.enter(parent);
        }
        let verdict = self
            .verdict(path, is_dir, |dir| dir.ignore.as_ref())
            .or_else(|| self.verdict(path, is_dir, |dir| dir.git.as_ref()));
        match verdict {
            Some(ignored) => !ignored,
            None => {
                let typed = (self.types.as_ref())
                    .is_some_and(|types| types.matched(path, is_dir).is_whitelist());
                typed || !(self.hide_hidden && path.file_name().is_some_and(is_hidden))
            }
        }
    }

    /// Make `chain` the directories from the workspace root down to `dir`.
    /// Consecutive entries of a walk share most of it, so only the directories
    /// the walk has just entered are read.
    fn enter(&mut self, dir: &Path) {
        if self.chain.last().is_some_and(|(last, _)| last == dir) {
            return;
        }
        while (self.chain.last()).is_some_and(|(last, _)| !dir.starts_with(last)) {
            self.chain.pop();
        }
        if self.chain.is_empty() {
            let root = self.root.clone();
            let rules = self.load(&root);
            self.chain.push((root, rules));
        }
        let mut at = self
            .chain
            .last()
            .map(|(last, _)| last.clone())
            .unwrap_or_default();
        let Ok(below) = dir.strip_prefix(&at) else {
            return;
        };
        for part in below.components() {
            at.push(part);
            let rules = self.load(&at);
            self.chain.push((at.clone(), rules));
        }
    }

    /// What the nearest matching rule of one kind says: `Some(true)` to
    /// ignore, `Some(false)` for a negation.
    fn verdict<F>(&self, path: &Path, is_dir: bool, pick: F) -> Option<bool>
    where
        F: Fn(&Dir) -> Option<&Gitignore>,
    {
        (self.chain.iter().rev())
            .filter_map(|(_, dir)| pick(dir))
            .map(|rules| rules.matched(path, is_dir))
            .find(|found| !found.is_none())
            .map(|found| found.is_ignore())
    }

    fn load(&mut self, dir: &Path) -> Dir {
        Dir {
            ignore: self.read(dir, ".ignore"),
            git: self.read(dir, ".gitignore"),
        }
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
