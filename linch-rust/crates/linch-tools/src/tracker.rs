//! Read-before-write: the one policy `Read`, `Write` and `Edit` share.
//!
//! `Read` records what a session saw of a file and the file's stamp then.
//! Overwriting an existing file needs a whole read at the current stamp;
//! editing needs the current stamp and a read of every line the edit
//! touches. A stamp change (anyone writing the file) voids the evidence, so
//! the model never changes content it has not seen.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

use linch_core::SessionId;
use thiserror::Error;

use crate::fs::FileStamp;

/// What one session has read, per workspace path. Shared by the file tools
/// of one workspace; safe to use from parallel `Read` calls.
///
/// Evidence lives in memory, so it is lost with the process; a resumed run
/// must read again before writing, which is the safe direction. The host
/// calls [`ReadTracker::forget`] when a session ends.
#[derive(Debug, Default)]
pub struct ReadTracker {
    sessions: Mutex<HashMap<SessionId, HashMap<String, Evidence>>>,
}

#[derive(Debug, Clone)]
struct Evidence {
    stamp: FileStamp,
    whole: bool,
    /// Sorted, merged, inclusive 1-based line ranges.
    ranges: Vec<(u64, u64)>,
}

/// What a read covered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Span {
    /// The whole file.
    Whole,
    /// Inclusive 1-based lines.
    Lines(u64, u64),
}

/// Why a write must wait for a read.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub(crate) enum ReadFirst {
    #[error("Read {path} before changing it.")]
    Unread { path: String },
    #[error("{path} changed since it was last read; Read it again.")]
    Stale { path: String },
    #[error("Read all of {path} before overwriting it.")]
    Partial { path: String },
    #[error("Read lines {start}-{end} of {path} before editing them.")]
    Lines { path: String, start: u64, end: u64 },
}

impl ReadTracker {
    /// An empty tracker.
    pub fn new() -> Self {
        Self::default()
    }

    /// Drop everything recorded for `session`.
    pub fn forget(&self, session: &SessionId) {
        self.lock().remove(session);
    }

    /// Record that `session` read `span` of `path` at `stamp`. Evidence from
    /// an older stamp is dropped first.
    pub(crate) fn record_read(
        &self,
        session: &SessionId,
        path: &str,
        stamp: FileStamp,
        span: Span,
    ) {
        let mut sessions = self.lock();
        let evidence = Self::current(sessions.entry(session.clone()).or_default(), path, stamp);
        match span {
            Span::Whole => evidence.whole = true,
            Span::Lines(start, end) => merge(&mut evidence.ranges, start, end.max(start)),
        }
    }

    /// Record that `session` wrote all of `path`, which now has `stamp`.
    pub(crate) fn record_write(&self, session: &SessionId, path: &str, stamp: FileStamp) {
        let mut sessions = self.lock();
        let evidence = Self::current(sessions.entry(session.clone()).or_default(), path, stamp);
        evidence.whole = true;
    }

    /// Record that `session` edited `path`, which now has `stamp`. Line
    /// ranges are dropped because lines may have moved; a whole read stays.
    pub(crate) fn record_edit(&self, session: &SessionId, path: &str, stamp: FileStamp) {
        let mut sessions = self.lock();
        let files = sessions.entry(session.clone()).or_default();
        let whole = files.get(path).is_some_and(|evidence| evidence.whole);
        files.insert(
            path.to_owned(),
            Evidence {
                stamp,
                whole,
                ranges: Vec::new(),
            },
        );
    }

    /// Check that `session` saw `path` at `stamp`: all of it when `lines` is
    /// `None`, else at least every given inclusive line range.
    pub(crate) fn check(
        &self,
        session: &SessionId,
        path: &str,
        stamp: FileStamp,
        lines: Option<&[(u64, u64)]>,
    ) -> Result<(), ReadFirst> {
        let sessions = self.lock();
        let Some(evidence) = sessions.get(session).and_then(|files| files.get(path)) else {
            return Err(ReadFirst::Unread {
                path: path.to_owned(),
            });
        };
        if evidence.stamp != stamp {
            return Err(ReadFirst::Stale {
                path: path.to_owned(),
            });
        }
        if evidence.whole {
            return Ok(());
        }
        let Some(lines) = lines else {
            return Err(ReadFirst::Partial {
                path: path.to_owned(),
            });
        };
        for &(start, end) in lines {
            let covered = evidence
                .ranges
                .iter()
                .any(|&(from, to)| from <= start && end <= to);
            if !covered {
                return Err(ReadFirst::Lines {
                    path: path.to_owned(),
                    start,
                    end,
                });
            }
        }
        Ok(())
    }

    /// The evidence for `path`, reset when its stamp is not `stamp`.
    fn current<'a>(
        files: &'a mut HashMap<String, Evidence>,
        path: &str,
        stamp: FileStamp,
    ) -> &'a mut Evidence {
        let evidence = files.entry(path.to_owned()).or_insert_with(|| Evidence {
            stamp,
            whole: false,
            ranges: Vec::new(),
        });
        if evidence.stamp != stamp {
            *evidence = Evidence {
                stamp,
                whole: false,
                ranges: Vec::new(),
            };
        }
        evidence
    }

    fn lock(&self) -> MutexGuard<'_, HashMap<SessionId, HashMap<String, Evidence>>> {
        self.sessions.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Insert `start..=end` into sorted, merged `ranges`.
fn merge(ranges: &mut Vec<(u64, u64)>, start: u64, end: u64) {
    ranges.push((start, end));
    ranges.sort_unstable();
    let mut merged: Vec<(u64, u64)> = Vec::with_capacity(ranges.len());
    for &(from, to) in ranges.iter() {
        match merged.last_mut() {
            Some(last) if from <= last.1.saturating_add(1) => last.1 = last.1.max(to),
            _ => merged.push((from, to)),
        }
    }
    *ranges = merged;
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use super::*;

    fn stamp(n: u64) -> FileStamp {
        FileStamp {
            modified: SystemTime::UNIX_EPOCH + Duration::from_secs(n),
            len: n,
        }
    }

    fn session() -> SessionId {
        SessionId::from("s1")
    }

    #[test]
    fn an_unread_file_cannot_change() {
        let reads = ReadTracker::new();
        let error = reads.check(&session(), "a", stamp(1), None).unwrap_err();
        assert!(matches!(error, ReadFirst::Unread { .. }));
        assert_eq!(error.to_string(), "Read a before changing it.");
    }

    #[test]
    fn a_whole_read_allows_any_change_until_the_stamp_moves() {
        let reads = ReadTracker::new();
        reads.record_read(&session(), "a", stamp(1), Span::Whole);
        assert_eq!(reads.check(&session(), "a", stamp(1), None), Ok(()));
        assert_eq!(
            reads.check(&session(), "a", stamp(1), Some(&[(9, 12)])),
            Ok(())
        );
        let stale = reads.check(&session(), "a", stamp(2), None).unwrap_err();
        assert!(matches!(stale, ReadFirst::Stale { .. }));
    }

    #[test]
    fn windowed_reads_cover_only_their_lines() {
        let reads = ReadTracker::new();
        reads.record_read(&session(), "a", stamp(1), Span::Lines(1, 10));
        reads.record_read(&session(), "a", stamp(1), Span::Lines(11, 20));
        assert_eq!(
            reads.check(&session(), "a", stamp(1), Some(&[(5, 15)])),
            Ok(())
        );
        let partial = reads.check(&session(), "a", stamp(1), None).unwrap_err();
        assert!(matches!(partial, ReadFirst::Partial { .. }));
        let lines = reads
            .check(&session(), "a", stamp(1), Some(&[(18, 21)]))
            .unwrap_err();
        assert_eq!(
            lines.to_string(),
            "Read lines 18-21 of a before editing them."
        );
    }

    #[test]
    fn a_read_at_a_new_stamp_drops_older_evidence() {
        let reads = ReadTracker::new();
        reads.record_read(&session(), "a", stamp(1), Span::Whole);
        reads.record_read(&session(), "a", stamp(2), Span::Lines(1, 2));
        let partial = reads.check(&session(), "a", stamp(2), None).unwrap_err();
        assert!(matches!(partial, ReadFirst::Partial { .. }));
    }

    #[test]
    fn an_edit_keeps_a_whole_read_but_drops_line_ranges() {
        let reads = ReadTracker::new();
        reads.record_read(&session(), "whole", stamp(1), Span::Whole);
        reads.record_edit(&session(), "whole", stamp(2));
        assert_eq!(reads.check(&session(), "whole", stamp(2), None), Ok(()));
        reads.record_read(&session(), "part", stamp(1), Span::Lines(1, 5));
        reads.record_edit(&session(), "part", stamp(2));
        assert_eq!(reads.check(&session(), "part", stamp(2), Some(&[])), Ok(()));
        assert!(
            reads
                .check(&session(), "part", stamp(2), Some(&[(1, 1)]))
                .is_err()
        );
    }

    #[test]
    fn a_write_counts_as_a_whole_read() {
        let reads = ReadTracker::new();
        reads.record_write(&session(), "a", stamp(3));
        assert_eq!(reads.check(&session(), "a", stamp(3), None), Ok(()));
    }

    #[test]
    fn sessions_are_separate_and_can_be_forgotten() {
        let reads = ReadTracker::new();
        reads.record_read(&session(), "a", stamp(1), Span::Whole);
        let other = SessionId::from("s2");
        assert!(reads.check(&other, "a", stamp(1), None).is_err());
        reads.forget(&session());
        assert!(reads.check(&session(), "a", stamp(1), None).is_err());
    }

    #[test]
    fn ranges_merge_when_they_touch() {
        let mut ranges = vec![(1, 3)];
        merge(&mut ranges, 4, 6);
        merge(&mut ranges, 10, 12);
        merge(&mut ranges, 2, 2);
        assert_eq!(ranges, [(1, 6), (10, 12)]);
    }
}
