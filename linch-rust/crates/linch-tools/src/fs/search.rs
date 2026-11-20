//! Blocking Grep and Glob for [`LocalFs`], on ripgrep's own crates: `ignore`
//! walks (gitignore, hidden files, type table, `--glob` overrides) and
//! `grep-searcher` searches, so no process is spawned.

use std::error::Error;
use std::io;
use std::path::{Component, Path};

use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{BinaryDetection, Searcher, SearcherBuilder, Sink, SinkContext, SinkMatch};
use ignore::overrides::{Override, OverrideBuilder};
use ignore::types::TypesBuilder;
use ignore::{DirEntry, WalkBuilder};
use tokio_util::sync::CancellationToken;

use super::beneath::{exhausted, stamp_of_stat};
use super::ignore_files::IgnoreFiles;
use super::local::is_hidden;
use super::{
    FsError, GlobHit, GlobHits, GlobQuery, GrepFile, GrepHits, GrepLine, GrepMode, GrepQuery,
};
use crate::fs::LocalFs;

/// Longest line text kept in content output, in bytes.
const MAX_LINE: usize = 500;

/// Search file contents under the query's path.
pub(super) fn grep(
    fs: &LocalFs,
    query: &GrepQuery,
    stop: &CancellationToken,
) -> Result<GrepHits, FsError> {
    let at = fs.locate(query.path.as_deref().unwrap_or("."))?;
    if !at.tail.is_empty() && at.leaf.is_none() {
        // Nothing there to search.
        return Ok(GrepHits::default());
    }
    let target = at.real_path(fs.root());
    let matcher = matcher(query)?;
    let limit = fs.search_limit();
    let mut searcher = searcher(query, limit);
    let overrides = match &query.glob {
        Some(glob) => OverrideBuilder::new(&target)
            .add(glob)
            .and_then(|builder| builder.build())
            .map_err(invalid)?,
        None => Override::empty(),
    };
    let (mut walk, rules) = walker(fs, &target, false, overrides);
    if let Some(name) = &query.file_type {
        let types = TypesBuilder::new()
            .add_defaults()
            .select(name)
            .build()
            .map_err(invalid)?;
        walk.types(types);
    }
    let mut hits = GrepHits::default();
    let mut lines = 0;
    let mut opener = fs.opener();
    for entry in walk.build() {
        rules.check()?;
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        let Some(path) = listed_file(entry, &mut hits.left_out)? else {
            continue;
        };
        // The walk only proposes names: every file is opened again from the
        // root, so a directory swapped for a symlink since it was listed
        // cannot lead the search out of the root. A name that cannot be
        // opened that way is counted, never named: it may come from outside.
        let Some(file) = opener.open(&path)? else {
            hits.left_out += 1;
            continue;
        };
        // A pattern that can match a newline makes the searcher read the
        // whole file; skip one over the cap without reading it. Other
        // searches stream, so any size is fine.
        if searcher.multi_line_with_matcher(&matcher)
            && file.metadata().is_ok_and(|meta| meta.len() > limit)
        {
            hits.skipped.push(fs.workspace_path(&path));
            continue;
        }
        let budget = match query.mode {
            GrepMode::Content => (query.limit - lines).saturating_add(1),
            _ => usize::MAX,
        };
        let mut sink = Collect::new(query.mode, budget);
        let searched = search(&mut searcher, &matcher, file, stop, &mut sink);
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        if sink.binary {
            continue;
        }
        // Over the memory limit or unreadable partway: keep what was found
        // before the cut, and say the file was not searched in full.
        if searched.is_err() {
            hits.skipped.push(fs.workspace_path(&path));
        }
        // Context lines alone mean the budget ran out before the match.
        if sink.count == 0 && sink.lines.is_empty() {
            continue;
        }
        lines += sink.lines.len();
        hits.files.push(GrepFile {
            path: fs.workspace_path(&path),
            count: if query.mode == GrepMode::Count {
                sink.count
            } else {
                0
            },
            lines: sink.lines,
        });
        let full = match query.mode {
            GrepMode::Content => lines > query.limit,
            _ => hits.files.len() > query.limit,
        };
        if full {
            hits.truncated = true;
            trim(&mut hits, query);
            break;
        }
    }
    rules.check()?;
    Ok(hits)
}

/// Every regular file under the root, walked with no filter and looked at
/// again from the root descriptor. Entries that cannot be read from the root
/// are left out; running out of descriptors is an error.
pub(super) fn files(fs: &LocalFs) -> Result<Vec<std::path::PathBuf>, FsError> {
    let mut opener = fs.opener();
    let mut walk = WalkBuilder::new(fs.root());
    walk.standard_filters(false).follow_links(false);
    let (mut found, mut left_out) = (Vec::new(), 0);
    for entry in walk.build() {
        if let Some(path) = listed_file(entry, &mut left_out)?
            && opener.stat(&path)?.is_some()
        {
            found.push(path);
        }
    }
    Ok(found)
}

/// List files under the query's path whose names match its pattern.
pub(super) fn glob(
    fs: &LocalFs,
    query: &GlobQuery,
    stop: &CancellationToken,
) -> Result<GlobHits, FsError> {
    let pattern = Path::new(&query.pattern);
    if pattern.is_absolute() || pattern.components().any(|c| c == Component::ParentDir) {
        return Err(FsError::InvalidQuery(
            "glob pattern must be relative and must not contain '..'".to_owned(),
        ));
    }
    let at = fs.locate(query.path.as_deref().unwrap_or("."))?;
    if !at.tail.is_empty() {
        return Err(FsError::NotADirectory {
            path: at.workspace_path(),
        });
    }
    let dir = at.real_path(fs.root());
    let dotted = pattern.components().any(|part| match part {
        Component::Normal(name) => is_hidden(name),
        _ => false,
    });
    let overrides = OverrideBuilder::new(&dir)
        .add(&query.pattern)
        .and_then(|builder| builder.build())
        .map_err(invalid)?;
    let (walk, rules) = walker(fs, &dir, dotted, overrides);
    let mut found = GlobHits::default();
    let mut opener = fs.opener();
    for entry in walk.build() {
        rules.check()?;
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        let Some(path) = listed_file(entry, &mut found.left_out)? else {
            continue;
        };
        // A name is listed only if it is a regular file under the root when
        // looked at again from the root descriptor; any other is counted.
        let Some(stat) = opener.stat(&path)? else {
            found.left_out += 1;
            continue;
        };
        found.hits.push(GlobHit {
            path: fs.workspace_path(&path),
            modified: Some(stamp_of_stat(&stat).modified),
        });
    }
    rules.check()?;
    Ok(found)
}

/// The walker both searches share, with the rules that drive it: sorted,
/// symlinks not followed, `.ignore` and `.gitignore` honoured outside git too
/// but read from the root descriptor (see [`IgnoreFiles`]), the host's global
/// ignore file not read.
fn walker(
    fs: &LocalFs,
    dir: &Path,
    hidden: bool,
    overrides: Override,
) -> (WalkBuilder, IgnoreFiles) {
    let rules = IgnoreFiles::new(fs.opener(), fs.root(), overrides.clone(), !hidden);
    let mut walk = WalkBuilder::new(dir);
    walk.follow_links(false)
        .overrides(overrides)
        .sort_by_file_name(|a, b| a.cmp(b));
    rules.install(&mut walk);
    (walk, rules)
}

/// The path of a walked regular file. Directories and symlinks yield `None`,
/// so a search never leaves the root through a link. An entry the walk could
/// not read is counted in `left_out`, unless the process is out of
/// descriptors, which is an error: an empty answer would look like a tree
/// with nothing in it.
fn listed_file(
    entry: Result<DirEntry, ignore::Error>,
    left_out: &mut usize,
) -> Result<Option<std::path::PathBuf>, FsError> {
    match entry {
        Ok(entry) => Ok(entry
            .file_type()
            .is_some_and(|kind| kind.is_file())
            .then(|| entry.into_path())),
        Err(error) => {
            if let Some(source) = error.io_error() {
                if exhausted_in(source) {
                    return Err(super::local::io_error(
                        ".",
                        io::Error::from(rustix::io::Errno::MFILE),
                    ));
                }
                *left_out += 1;
            }
            Ok(None)
        }
    }
}

/// Whether `error`, or an error it wraps (the walk wraps the system's), is
/// the process running out of descriptors.
fn exhausted_in(error: &io::Error) -> bool {
    if exhausted(error) {
        return true;
    }
    let mut next = error.get_ref().map(|inner| inner as &(dyn Error + 'static));
    while let Some(inner) = next {
        if inner.downcast_ref::<io::Error>().is_some_and(exhausted) {
            return true;
        }
        next = inner.source();
    }
    false
}

/// The searcher for `query`. It holds at most `limit` bytes of a file: the
/// whole file in multiline mode, else the longest line with its context. A
/// file that needs more fails, so one search cannot exhaust memory.
fn searcher(query: &GrepQuery, limit: u64) -> Searcher {
    let content = query.mode == GrepMode::Content;
    SearcherBuilder::new()
        .line_number(true)
        .multi_line(query.multiline)
        .binary_detection(BinaryDetection::quit(0))
        .heap_limit(usize::try_from(limit).ok())
        .before_context(if content { query.before } else { 0 })
        .after_context(if content { query.after } else { 0 })
        .build()
}

/// Search one file's contents into `sink`, failing once `stop` fires.
fn search(
    searcher: &mut Searcher,
    matcher: &RegexMatcher,
    file: impl io::Read,
    stop: &CancellationToken,
    sink: &mut Collect,
) -> io::Result<()> {
    searcher.search_reader(matcher, Stoppable { file, stop }, sink)
}

/// A reader that fails once `stop` fires, so a long file is not read to
/// the end after the call is cancelled.
struct Stoppable<'a, R> {
    file: R,
    stop: &'a CancellationToken,
}

impl<R: io::Read> io::Read for Stoppable<'_, R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.stop.is_cancelled() {
            return Err(io::Error::other("search cancelled"));
        }
        self.file.read(buf)
    }
}

fn matcher(query: &GrepQuery) -> Result<RegexMatcher, FsError> {
    let mut builder = RegexMatcherBuilder::new();
    builder.case_insensitive(query.case_insensitive);
    if query.multiline {
        builder.multi_line(true).dot_matches_new_line(true);
    } else {
        builder.line_terminator(Some(b'\n'));
    }
    builder.build(&query.pattern).map_err(invalid)
}

fn invalid(error: impl std::fmt::Display) -> FsError {
    FsError::InvalidQuery(error.to_string())
}

/// Cut a truncated result back to exactly `limit` entries.
fn trim(hits: &mut GrepHits, query: &GrepQuery) {
    if query.mode != GrepMode::Content {
        hits.files.truncate(query.limit);
        return;
    }
    let mut left = query.limit;
    hits.files.retain_mut(|file| {
        file.lines.truncate(left);
        left -= file.lines.len();
        !file.lines.is_empty()
    });
}

/// Collects one file's results.
struct Collect {
    mode: GrepMode,
    budget: usize,
    count: u64,
    lines: Vec<GrepLine>,
    binary: bool,
}

impl Collect {
    fn new(mode: GrepMode, budget: usize) -> Self {
        Self {
            mode,
            budget,
            count: 0,
            lines: Vec::new(),
            binary: false,
        }
    }

    /// Record lines starting at `number`; `false` once the budget is spent.
    fn push(&mut self, number: Option<u64>, bytes: &[u8], is_match: bool) -> bool {
        let first = number.unwrap_or(0);
        for (number, line) in (first..).zip(bytes.split_inclusive(|&byte| byte == b'\n')) {
            if self.lines.len() >= self.budget {
                return false;
            }
            self.lines.push(GrepLine {
                number,
                text: line_text(line),
                is_match,
            });
        }
        self.lines.len() < self.budget
    }
}

impl Sink for Collect {
    type Error = io::Error;

    fn matched(&mut self, _: &Searcher, found: &SinkMatch<'_>) -> Result<bool, io::Error> {
        self.count += found.lines().count() as u64;
        Ok(match self.mode {
            GrepMode::FilesWithMatches => false,
            GrepMode::Count => true,
            GrepMode::Content => self.push(found.line_number(), found.bytes(), true),
        })
    }

    fn context(&mut self, _: &Searcher, context: &SinkContext<'_>) -> Result<bool, io::Error> {
        Ok(self.push(context.line_number(), context.bytes(), false))
    }

    fn binary_data(&mut self, _: &Searcher, _: u64) -> Result<bool, io::Error> {
        self.binary = true;
        Ok(false)
    }
}

/// A line without its terminator, decoded lossily and cut at [`MAX_LINE`].
fn line_text(line: &[u8]) -> String {
    let line = line.strip_suffix(b"\n").unwrap_or(line);
    let line = line.strip_suffix(b"\r").unwrap_or(line);
    let text = String::from_utf8_lossy(line);
    if text.len() <= MAX_LINE {
        return text.into_owned();
    }
    let mut cut = MAX_LINE;
    while !text.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{} [... line truncated]", &text[..cut])
}

#[cfg(test)]
mod tests;
