//! Which ignore files `Grep` and `Glob` honour and how: they are read from
//! the workspace root descriptor (never through a symlink), and keep the
//! precedence of ripgrep's walk.

mod common;

use std::fs;
use std::os::unix::fs::symlink;

use common::Workspace;
use linch_tools::fs::{ExecutionFs, GlobQuery, GrepMode, GrepQuery};
use tokio_util::sync::CancellationToken;

fn needle(path: Option<&str>) -> GrepQuery {
    GrepQuery {
        pattern: "needle".to_owned(),
        path: path.map(str::to_owned),
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 1000,
    }
}

async fn grepped(ws: &Workspace, path: Option<&str>) -> Vec<String> {
    grepped_with(ws, needle(path)).await
}

async fn grepped_with(ws: &Workspace, query: GrepQuery) -> Vec<String> {
    let hits = ws.fs.grep(&query, &CancellationToken::new()).await.unwrap();
    assert_eq!(hits.left_out, 0);
    hits.files.into_iter().map(|file| file.path).collect()
}

async fn globbed(ws: &Workspace, pattern: &str, path: Option<&str>) -> Vec<String> {
    let query = GlobQuery {
        pattern: pattern.to_owned(),
        path: path.map(str::to_owned),
    };
    let found = ws.fs.glob(&query, &CancellationToken::new()).await.unwrap();
    assert_eq!(found.left_out, 0);
    found.hits.into_iter().map(|hit| hit.path).collect()
}

/// Nested ignore files, negation, `.ignore` over `.gitignore`, hidden files.
#[tokio::test]
async fn ignore_files_keep_their_precedence() {
    let ws = Workspace::new();
    ws.put(".gitignore", "a.txt\nbuild/\n*.log\n!keep.log\n");
    for name in [
        "a.txt",
        "keep.log",
        "drop.log",
        "build/x.txt",
        "top.txt",
        "src/b.txt",
        "src/c.txt",
        "src/d.txt",
        ".hidden.txt",
    ] {
        ws.put(name, "needle\n");
    }
    ws.put("src/.gitignore", "b.txt\nd.txt\n");
    ws.put("src/.ignore", "!b.txt\nc.txt\n");
    let want = ["keep.log", "src/b.txt", "top.txt"];
    assert_eq!(grepped(&ws, None).await, want);
    assert_eq!(globbed(&ws, "**/.hid*", None).await, [".hidden.txt"]);
}

/// A search below the root still honours the ignore files above it.
#[tokio::test]
async fn a_search_of_a_subdirectory_honours_the_ignore_files_above_it() {
    let ws = Workspace::new();
    ws.put(".gitignore", "*.tmp\n");
    ws.put("sub/.gitignore", "z.txt\n");
    for name in ["sub/x.tmp", "sub/y.txt", "sub/z.txt", "sub/deeper/w.tmp"] {
        ws.put(name, "needle\n");
    }
    assert_eq!(grepped(&ws, Some("sub")).await, ["sub/y.txt"]);
}

/// A glob pattern overrides ignore files (also for hidden names), and a
/// negation rescues a hidden name from a grep.
#[tokio::test]
async fn a_glob_pattern_and_a_negation_beat_the_ignore_files_they_should() {
    let ws = Workspace::new();
    ws.put(".gitignore", "a.txt\n!.env\n");
    ws.put("a.txt", "needle\n");
    ws.put(".env", "needle\n");
    assert_eq!(globbed(&ws, "a.txt", None).await, ["a.txt"]);
    assert_eq!(grepped(&ws, None).await, [".env"]);
}

/// An ignore file that is a symlink is not read, whatever it points at: a
/// search never answers according to the content of a file outside the root.
#[tokio::test]
async fn a_search_does_not_read_a_host_file_through_a_symlinked_ignore_file() {
    let all = [
        "d/guess-one.txt",
        "d/guess-two.txt",
        "guess-one.txt",
        "guess-two.txt",
    ];
    for name in [".ignore", ".gitignore"] {
        for dir in ["", "d/"] {
            let ws = Workspace::new();
            let host = ws.outside().join("host-file");
            fs::write(&host, "guess-one.txt\n").unwrap();
            for file in all {
                ws.put(file, "needle\n");
            }
            symlink(&host, ws.root().join(dir).join(name)).unwrap();
            assert_eq!(grepped(&ws, None).await, all, "{dir}{name}");
        }
    }
}

fn rust_files(ws: &Workspace) {
    for name in [".top.rs", "src/.plain.rs", "src/m.rs", "src/n.txt"] {
        ws.put(name, "needle\n");
    }
}

/// A file type whitelists a name, which exempts a hidden file of that type
/// from the hidden filter; an ignore rule still beats the type.
#[tokio::test]
async fn a_file_type_finds_the_hidden_files_of_that_type() {
    let ws = Workspace::new();
    rust_files(&ws);
    let query = GrepQuery {
        file_type: Some("rust".to_owned()),
        ..needle(None)
    };
    let all = [".top.rs", "src/.plain.rs", "src/m.rs"];
    assert_eq!(grepped_with(&ws, query.clone()).await, all);
    ws.put(".gitignore", "m.rs\n.plain.rs\n");
    assert_eq!(grepped_with(&ws, query).await, [".top.rs"]);
    // Without the type, the hidden files stay hidden.
    assert_eq!(grepped(&ws, None).await, ["src/n.txt"]);
}

/// A `--glob` pattern beats the hidden filter too.
#[tokio::test]
async fn a_grep_glob_finds_the_hidden_files_it_matches() {
    let ws = Workspace::new();
    rust_files(&ws);
    let query = GrepQuery {
        glob: Some("*.rs".to_owned()),
        ..needle(None)
    };
    assert_eq!(
        grepped_with(&ws, query).await,
        [".top.rs", "src/.plain.rs", "src/m.rs"]
    );
}

/// The rules of a directory apply to it and below, not to its siblings or to
/// the entries that follow it in the parent.
#[tokio::test]
async fn ignore_rules_do_not_leak_between_sibling_directories() {
    let ws = Workspace::new();
    ws.put("a/.gitignore", "x.txt\n");
    ws.put("a/sub/.gitignore", "y.txt\n");
    for name in [
        "a/x.txt",
        "a/y.txt",
        "a/sub/x.txt",
        "a/sub/y.txt",
        "a/z.txt",
        "b/x.txt",
        "b/y.txt",
        "x.txt",
    ] {
        ws.put(name, "needle\n");
    }
    assert_eq!(
        grepped(&ws, None).await,
        ["a/y.txt", "a/z.txt", "b/x.txt", "b/y.txt", "x.txt"]
    );
}
