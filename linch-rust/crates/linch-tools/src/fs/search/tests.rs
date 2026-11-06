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
    search(&mut searcher(query, CAP), &matcher, input, stop, &mut sink)
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

/// The search memory limit in the tests below, so no big file is needed.
const LIMIT: u64 = 1024;

/// A temp root holding `files`, searched with [`LIMIT`].
fn workspace(files: &[(&str, &str)]) -> (tempfile::TempDir, LocalFs) {
    let dir = tempfile::tempdir().unwrap();
    for (name, content) in files {
        std::fs::write(dir.path().join(name), content).unwrap();
    }
    let fs = LocalFs::new(dir.path()).unwrap().with_search_limit(LIMIT);
    (dir, fs)
}

fn paths(hits: &GrepHits) -> Vec<&str> {
    hits.files.iter().map(|file| file.path.as_str()).collect()
}

/// Three matches, then a line over the limit, then a fourth match.
fn cut_log() -> String {
    format!(
        "needle 1\nneedle 2\nneedle 3\n{}\nneedle 4\n",
        "a".repeat(2048)
    )
}

#[test]
fn a_file_cut_off_by_the_limit_keeps_its_earlier_matches() {
    let log = cut_log();
    let (_dir, fs) = workspace(&[("app.log", &log), ("small.txt", "needle small\n")]);
    let stop = CancellationToken::new();
    let mut query = query("needle", false);

    let hits = grep(&fs, &query, &stop).unwrap();
    assert_eq!(paths(&hits), ["app.log", "small.txt"]);
    let kept: Vec<&str> = hits.files[0]
        .lines
        .iter()
        .map(|l| l.text.as_str())
        .collect();
    assert_eq!(kept, ["needle 1", "needle 2", "needle 3"]);
    assert_eq!(hits.skipped, ["app.log"]);

    query.mode = GrepMode::Count;
    let hits = grep(&fs, &query, &stop).unwrap();
    let counts: Vec<u64> = hits.files.iter().map(|file| file.count).collect();
    assert_eq!(paths(&hits), ["app.log", "small.txt"]);
    assert_eq!(counts, [3, 1]);
    assert_eq!(hits.skipped, ["app.log"]);
}

#[test]
fn multiline_size_skips_are_reported() {
    let big = format!("start\nend\n{}", "x\n".repeat(1024));
    let (_dir, fs) = workspace(&[("big.txt", &big), ("small.txt", "start\nend\n")]);
    let mut query = query("start.end", true);
    query.mode = GrepMode::FilesWithMatches;
    let hits = grep(&fs, &query, &CancellationToken::new()).unwrap();
    assert_eq!(paths(&hits), ["small.txt"]);
    assert_eq!(hits.skipped, ["big.txt"]);
}

/// Run the Grep tool over `fs` and return its text.
async fn run_grep(fs: LocalFs, input: serde_json::Value) -> String {
    use linch_core::{RunId, SessionId, Tool, ToolContext};

    let tool = crate::Grep::new(std::sync::Arc::new(fs));
    let ctx = ToolContext::new(
        SessionId::from("s"),
        RunId::from("r"),
        "t",
        None,
        CancellationToken::new(),
        None,
    );
    let input = input.as_object().unwrap().clone();
    let output = tool.execute(input, ctx).await.unwrap();
    output.value.as_str().unwrap().to_owned()
}

#[tokio::test]
async fn grep_names_the_files_it_could_not_search_in_full() {
    let log = cut_log();
    let (_dir, fs) = workspace(&[("app.log", &log), ("small.txt", "needle small\n")]);
    let content = serde_json::json!({"pattern": "needle", "output_mode": "content"});
    assert_eq!(
        run_grep(fs, content).await,
        "app.log:needle 1\napp.log:needle 2\napp.log:needle 3\nsmall.txt:needle small\n\
         … (not searched in full, so matches may be missing: app.log)"
    );
}

#[tokio::test]
async fn before_context_over_the_limit_is_reported_not_hidden() {
    // 30 lines of 50 bytes of context need more than the limit.
    let line = format!("{}\n", "b".repeat(49));
    let body = format!("{}needle\n", line.repeat(30));
    let (_dir, fs) = workspace(&[("ctx.txt", &body)]);
    let wide = serde_json::json!({"pattern": "needle", "output_mode": "content", "-B": 30});
    assert_eq!(
        run_grep(fs, wide).await,
        "No matches found.\n… (not searched in full, so matches may be missing: ctx.txt)"
    );
}
