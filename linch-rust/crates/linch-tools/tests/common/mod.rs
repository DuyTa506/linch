//! Helpers shared by the tool tests: a temp workspace and call contexts.

#![allow(dead_code)]

use std::fs;
use std::path::PathBuf;
use std::sync::Arc;

use linch_core::{JsonObject, RunId, SessionId, Tool, ToolContext, ToolResult};
use linch_tools::{LocalFs, ReadTracker};
use tempfile::TempDir;
use tokio_util::sync::CancellationToken;

/// A temp dir with `root/` (the workspace) and `outside/` beside it.
pub struct Workspace {
    pub dir: TempDir,
    pub fs: Arc<LocalFs>,
    pub reads: Arc<ReadTracker>,
}

impl Workspace {
    pub fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("root")).unwrap();
        fs::create_dir_all(dir.path().join("outside")).unwrap();
        let fs = Arc::new(LocalFs::new(dir.path().join("root")).unwrap());
        Self {
            dir,
            fs,
            reads: Arc::new(ReadTracker::new()),
        }
    }

    pub fn root(&self) -> PathBuf {
        self.fs.root().to_path_buf()
    }

    pub fn outside(&self) -> PathBuf {
        self.dir.path().join("outside")
    }

    /// Write `content` to `path` under the root, creating parents.
    pub fn put(&self, path: &str, content: &str) {
        let full = self.root().join(path);
        fs::create_dir_all(full.parent().unwrap()).unwrap();
        fs::write(full, content).unwrap();
    }

    pub fn get(&self, path: &str) -> String {
        fs::read_to_string(self.root().join(path)).unwrap()
    }
}

/// A context for session `session` with a fresh cancel token.
pub fn ctx(session: &str) -> ToolContext {
    ToolContext::new(
        SessionId::from(session),
        RunId::from("run"),
        "toolu_1",
        None,
        CancellationToken::new(),
        None,
    )
}

/// `value` as a tool input object.
pub fn input(value: serde_json::Value) -> JsonObject {
    match value {
        serde_json::Value::Object(object) => object,
        _ => panic!("input must be an object"),
    }
}

/// Run `tool` in session `s1`.
pub async fn call(tool: &dyn Tool, value: serde_json::Value) -> ToolResult {
    tool.execute(input(value), ctx("s1")).await
}

/// The text of a successful result.
pub fn text(result: ToolResult) -> String {
    let output = result.expect("tool call succeeds");
    output.value.as_str().expect("text output").to_owned()
}

/// The code of a failed result.
pub fn code(result: ToolResult) -> String {
    let error = result.expect_err("tool call fails");
    error.code.unwrap_or_default()
}
