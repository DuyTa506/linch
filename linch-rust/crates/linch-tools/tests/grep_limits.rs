//! Grep's bounds on model input and on the memory one search may use.

mod common;

use std::fs::File;
use std::sync::{Arc, Mutex, PoisonError};

use async_trait::async_trait;
use common::{Workspace, call, text};
use linch_tools::fs::{FileContent, FileInfo, FileStamp, GlobHit, GlobQuery, GrepHits, GrepQuery};
use linch_tools::{ExecutionFs, FsError, Grep};
use serde_json::json;
use tokio_util::sync::CancellationToken;

/// Records the last Grep query and finds nothing.
#[derive(Default)]
struct Recording(Mutex<Option<GrepQuery>>);

impl Recording {
    fn last(&self) -> GrepQuery {
        let last = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        last.clone().expect("a query was made")
    }
}

#[async_trait]
impl ExecutionFs for Recording {
    async fn stat(&self, _: &str) -> Result<FileInfo, FsError> {
        unreachable!()
    }

    async fn read(&self, _: &str, _: u64) -> Result<FileContent, FsError> {
        unreachable!()
    }

    async fn write(&self, _: &str, _: Vec<u8>, _: Option<FileStamp>) -> Result<FileStamp, FsError> {
        unreachable!()
    }

    async fn grep(&self, query: &GrepQuery, _: &CancellationToken) -> Result<GrepHits, FsError> {
        *self.0.lock().unwrap_or_else(PoisonError::into_inner) = Some(query.clone());
        Ok(GrepHits::default())
    }

    async fn glob(&self, _: &GlobQuery, _: &CancellationToken) -> Result<Vec<GlobHit>, FsError> {
        unreachable!()
    }
}

#[tokio::test]
async fn the_largest_head_limit_still_returns_matches() {
    let ws = Workspace::new();
    ws.put("a.txt", "m\nm\n");
    let tool = Grep::new(ws.fs.clone());
    let lines = json!({"pattern": "m", "output_mode": "content", "head_limit": u64::MAX});
    assert_eq!(text(call(&tool, lines).await), "a.txt:m\na.txt:m");
    let files = json!({"pattern": "m", "head_limit": u64::MAX});
    assert_eq!(text(call(&tool, files).await), "a.txt");
}

#[tokio::test]
async fn multiline_search_skips_a_file_over_the_read_cap() {
    let ws = Workspace::new();
    ws.put("small.txt", "start\nend\n");
    // Sparse: a text head longer than the binary check, then a hole up to
    // one byte past Read's 10 MiB cap.
    ws.put("big.txt", &format!("start\nend\n{}", "x\n".repeat(40_000)));
    let big = File::options()
        .write(true)
        .open(ws.root().join("big.txt"))
        .unwrap();
    big.set_len(10 * 1024 * 1024 + 1).unwrap();
    let tool = Grep::new(ws.fs.clone());
    let multiline = json!({"pattern": "start.end", "multiline": true});
    assert_eq!(
        text(call(&tool, multiline).await),
        "small.txt\n… (not searched in full, so matches may be missing: big.txt)"
    );
    // A line search streams the file, so it still finds the match, and so
    // does a multiline one whose pattern cannot match a newline.
    let lines = json!({"pattern": "start"});
    assert_eq!(text(call(&tool, lines).await), "big.txt\nsmall.txt");
    let single = json!({"pattern": "start", "multiline": true});
    assert_eq!(text(call(&tool, single).await), "big.txt\nsmall.txt");
}

#[tokio::test]
async fn head_limit_and_context_lines_are_bounded() {
    let fs = Arc::new(Recording::default());
    let tool = Grep::new(fs.clone());
    let huge = json!({"pattern": "m", "head_limit": u64::MAX, "-A": u64::MAX, "-B": u64::MAX});
    call(&tool, huge).await.unwrap();
    let query = fs.last();
    // Every entry takes at least two bytes, so no more fit in the output cap.
    assert_eq!(
        (query.limit, query.before, query.after),
        (15_000, 15_000, 15_000)
    );
    call(&tool, json!({"pattern": "m", "-C": u64::MAX}))
        .await
        .unwrap();
    let query = fs.last();
    assert_eq!(
        (query.limit, query.before, query.after),
        (250, 15_000, 15_000)
    );
    let small = json!({"pattern": "m", "head_limit": 3, "-B": 2});
    call(&tool, small).await.unwrap();
    let query = fs.last();
    assert_eq!((query.limit, query.before, query.after), (3, 2, 0));
}
