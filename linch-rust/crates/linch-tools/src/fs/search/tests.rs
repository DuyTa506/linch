//! One file's search against inputs too big to write to disk in a test.

use std::io::{self, Read};

use super::*;

/// Read's cap, which a search may hold of one file.
const CAP: u64 = crate::read::MAX_READ_BYTES;

fn query(pattern: &str, multiline: bool) -> GrepQuery {
    GrepQuery {
        pattern: pattern.to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::Content,
        case_insensitive: false,
        multiline,
        before: 0,
        after: 0,
        limit: 10,
    }
}

/// Search `input` as if it were one file's contents.
fn search_input(query: &GrepQuery, input: impl Read, stop: &CancellationToken) -> io::Result<()> {
    let mut sink = Collect::new(query.mode, query.limit + 1);
    let matcher = matcher(query).unwrap();
    search(&mut searcher(query), &matcher, input, stop, &mut sink)
}

#[test]
fn a_line_longer_than_the_cap_fails_the_file() {
    let line = io::repeat(b'a').take(CAP + 1);
    assert!(search_input(&query("a", false), line, &CancellationToken::new()).is_err());
}

#[test]
fn a_multiline_search_holds_no_more_than_the_cap() {
    // A pattern that never matches, so the search is one pass.
    let file = io::repeat(b'a').take(CAP + 1);
    assert!(search_input(&query("b.b", true), file, &CancellationToken::new()).is_err());
}

#[test]
fn a_search_stops_inside_one_long_file_once_cancelled() {
    let stop = CancellationToken::new();
    stop.cancel();
    let lines = io::repeat(b'\n').take(64 << 20);
    assert!(search_input(&query("x", false), lines, &stop).is_err());
}
