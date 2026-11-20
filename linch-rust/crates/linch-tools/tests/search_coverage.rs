//! No empty answer when descriptors run out. This binary holds one test: it
//! lowers the descriptor limit of the whole process.

mod common;

use std::fs::{self, File};

use common::Workspace;
use linch_tools::fs::{ExecutionFs, FsError, GlobQuery, GrepMode, GrepQuery};
use rustix::process::{Resource, Rlimit, getrlimit, setrlimit};
use tokio_util::sync::CancellationToken;

/// Descriptors are used up: a search says so instead of answering with
/// nothing, and so does a deep read. Runs alone in this binary because the
/// limit is the whole process's.
#[tokio::test]
async fn a_search_without_descriptors_is_an_error_not_an_empty_answer() {
    let ws = Workspace::new();
    let deep = vec!["a"; 60].join("/");
    ws.put(&format!("{deep}/f.txt"), "needle\n");
    ws.put("top.txt", "needle\n");
    let cancel = CancellationToken::new();
    let query = GrepQuery {
        pattern: "needle".to_owned(),
        path: None,
        glob: None,
        file_type: None,
        mode: GrepMode::FilesWithMatches,
        case_insensitive: false,
        multiline: false,
        before: 0,
        after: 0,
        limit: 100,
    };
    let glob = GlobQuery {
        pattern: "**/*.txt".to_owned(),
        path: None,
    };
    // Warm the blocking pool and the walk's allocations.
    ws.fs.grep(&query, &cancel).await.unwrap();
    ws.fs.glob(&glob, &cancel).await.unwrap();
    let before = getrlimit(Resource::Nofile);
    let open = fs::read_dir("/proc/self/fd").unwrap().count();
    let limit = Rlimit {
        current: Some(u64::try_from(open + 24).unwrap()),
        maximum: before.maximum,
    };
    setrlimit(Resource::Nofile, limit).unwrap();
    // 60 levels need 60 descriptors; about 24 are left.
    let deep_read = ws.fs.read(&format!("{deep}/f.txt"), 1024).await;
    let held: Vec<File> = std::iter::from_fn(|| File::open("/dev/null").ok()).collect();
    let grep = ws.fs.grep(&query, &cancel).await;
    let globbed = ws.fs.glob(&glob, &cancel).await;
    drop(held);
    setrlimit(Resource::Nofile, before).unwrap();
    assert!(
        matches!(deep_read, Err(FsError::Io { .. })),
        "a read that needs more descriptors than are left: {deep_read:?}"
    );
    assert!(
        matches!(grep, Err(FsError::Io { .. })),
        "Grep with no descriptor left: {grep:?}"
    );
    assert!(
        matches!(globbed, Err(FsError::Io { .. })),
        "Glob with no descriptor left: {globbed:?}"
    );
}
