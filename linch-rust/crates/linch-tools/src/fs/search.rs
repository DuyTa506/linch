//! Blocking Grep and Glob for [`LocalFs`], on ripgrep's own crates: `ignore`
//! walks (gitignore, hidden files, type table, `--glob` overrides) and
//! `grep-searcher` searches, so no process is spawned.

use std::fs::File;
use std::io;
use std::path::{Component, Path};

use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{BinaryDetection, Searcher, SearcherBuilder, Sink, SinkContext, SinkMatch};
use ignore::overrides::OverrideBuilder;
use ignore::types::TypesBuilder;
use ignore::{DirEntry, WalkBuilder};
use tokio_util::sync::CancellationToken;

use super::local::is_hidden;
use super::{FsError, GlobHit, GlobQuery, GrepFile, GrepHits, GrepLine, GrepMode, GrepQuery};
use crate::fs::LocalFs;
use crate::read::MAX_READ_BYTES;

/// Longest line text kept in content output, in bytes.
const MAX_LINE: usize = 500;

/// Search file contents under the query's path.
pub(super) fn grep(
    fs: &LocalFs,
    query: &GrepQuery,
    stop: &CancellationToken,
) -> Result<GrepHits, FsError> {
    let target = fs.resolve(query.path.as_deref().unwrap_or("."))?;
    let matcher = matcher(query)?;
    let mut searcher = searcher(query);
    let mut walk = walker(&target, false);
    if let Some(glob) = &query.glob {
        let overrides = OverrideBuilder::new(&target)
            .add(glob)
            .and_then(|builder| builder.build())
            .map_err(invalid)?;
        walk.overrides(overrides);
    }
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
    for entry in walk.build() {
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        let Some(path) = regular_file(entry) else {
            continue;
        };
        // A multiline search reads the whole file; skip one over the cap
        // without reading it. Line searches stream, so any size is fine.
        if query.multiline
            && path
                .metadata()
                .is_ok_and(|meta| meta.len() > MAX_READ_BYTES)
        {
            continue;
        }
        let budget = match query.mode {
            GrepMode::Content => (query.limit - lines).saturating_add(1),
            _ => usize::MAX,
        };
        let mut sink = Collect::new(query.mode, budget);
        let searched = File::open(&path)
            .and_then(|file| search(&mut searcher, &matcher, file, stop, &mut sink));
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        if searched.is_err() || sink.binary {
            continue;
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
    Ok(hits)
}

/// List files under the query's path whose names match its pattern.
pub(super) fn glob(
    fs: &LocalFs,
    query: &GlobQuery,
    stop: &CancellationToken,
) -> Result<Vec<GlobHit>, FsError> {
    let pattern = Path::new(&query.pattern);
    if pattern.is_absolute() || pattern.components().any(|c| c == Component::ParentDir) {
        return Err(FsError::InvalidQuery(
            "glob pattern must be relative and must not contain '..'".to_owned(),
        ));
    }
    let dir = fs.resolve(query.path.as_deref().unwrap_or("."))?;
    if !dir.is_dir() {
        return Err(FsError::NotADirectory {
            path: fs.workspace_path(&dir),
        });
    }
    let dotted = pattern.components().any(|part| match part {
        Component::Normal(name) => is_hidden(name),
        _ => false,
    });
    let overrides = OverrideBuilder::new(&dir)
        .add(&query.pattern)
        .and_then(|builder| builder.build())
        .map_err(invalid)?;
    let mut walk = walker(&dir, dotted);
    walk.overrides(overrides);
    let mut hits = Vec::new();
    for entry in walk.build() {
        if stop.is_cancelled() {
            return Err(FsError::Cancelled);
        }
        let Some(path) = regular_file(entry) else {
            continue;
        };
        let modified = path.metadata().and_then(|meta| meta.modified()).ok();
        hits.push(GlobHit {
            path: fs.workspace_path(&path),
            modified,
        });
    }
    Ok(hits)
}

/// The walker both searches share: sorted, symlinks not followed, the
/// host's global gitignore not read, `.gitignore` honoured outside git too.
fn walker(dir: &Path, hidden: bool) -> WalkBuilder {
    let mut walk = WalkBuilder::new(dir);
    walk.hidden(!hidden)
        .follow_links(false)
        .git_global(false)
        .require_git(false)
        .sort_by_file_name(|a, b| a.cmp(b));
    walk
}

/// The path of a walked regular file. Directories, symlinks and unreadable
/// entries yield `None`, so a search never leaves the root through a link.
fn regular_file(entry: Result<DirEntry, ignore::Error>) -> Option<std::path::PathBuf> {
    let entry = entry.ok()?;
    entry
        .file_type()
        .is_some_and(|kind| kind.is_file())
        .then(|| entry.into_path())
}

/// The searcher for `query`. It holds at most [`MAX_READ_BYTES`] of a file:
/// the whole file in multiline mode, else the longest line with its context.
/// A file that needs more fails, so one search cannot exhaust memory.
fn searcher(query: &GrepQuery) -> Searcher {
    let content = query.mode == GrepMode::Content;
    SearcherBuilder::new()
        .line_number(true)
        .multi_line(query.multiline)
        .binary_detection(BinaryDetection::quit(0))
        .heap_limit(usize::try_from(MAX_READ_BYTES).ok())
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
