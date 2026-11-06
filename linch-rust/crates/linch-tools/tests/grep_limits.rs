//! Grep's bounds on model input and on the memory one search may use.

mod common;

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
