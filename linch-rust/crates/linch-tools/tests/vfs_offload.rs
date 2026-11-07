//! `ResultOffload`: oversized tool results go to the session's backend and
//! the model sees a preview plus a pointer.

use std::sync::Arc;

use async_trait::async_trait;
use linch_core::hooks::{HookChain, PostToolUse, PostToolUseRequest, PostToolUseResponse};
use linch_core::{
    FailMode, Hook, HookStatus, JsonObject, JsonValue, RunId, SessionId, ToolOutput,
    ToolOutputError, ToolResult,
};
use linch_tools::vfs::{
    FileBackend, MemorySessions, OffloadConfig, ResultOffload, SessionFiles, SharedFiles, VfsError,
};

/// `n` numbered lines, `line 1` to `line n`, about 8 bytes each.
fn lines(n: usize) -> String {
    let all: Vec<String> = (1..=n).map(|i| format!("line {i}")).collect();
    all.join("\n")
}

fn request(tool: &str, result: ToolResult) -> PostToolUseRequest {
    PostToolUseRequest {
        session_id: SessionId::from("s1"),
        run_id: RunId::from("r1"),
        tool_use_id: "call_7".into(),
        tool_name: tool.into(),
        input: JsonObject::new(),
        result,
    }
}

fn offload(files: Arc<dyn SessionFiles>, threshold: u64) -> ResultOffload {
    ResultOffload::new(files, OffloadConfig::new(threshold)).unwrap()
}

/// The text of a replacement, or `None` for `Continue`.
fn replaced(response: PostToolUseResponse) -> Option<ToolOutput> {
    match response {
        PostToolUseResponse::Continue => None,
        PostToolUseResponse::Replace { result } => Some(result.expect("a success result")),
    }
}

#[test]
fn the_default_config_matches_python() {
    let config = OffloadConfig::new(5_000);
    assert_eq!(config.threshold_tokens, 5_000);
    assert_eq!(config.preview_lines, 10);
    assert_eq!(config.path_prefix, "/offload");
    let skipped: Vec<&str> = config.skip_tools.iter().map(String::as_str).collect();
    assert_eq!(skipped, ["edit_file", "ls", "read_file", "write_file"]);
}

#[tokio::test]
async fn an_oversized_result_is_stored_and_replaced_by_a_pointer() {
    let sessions = Arc::new(MemorySessions::new());
    let hook = offload(sessions.clone(), 100);
    assert_eq!(hook.fail_mode(), Some(FailMode::Open));
    let payload = lines(200);
    let mut output = ToolOutput::new(payload.clone());
    output.metadata.insert("source".into(), "search".into());
    let response = hook.call(&request("web_search", Ok(output))).await.unwrap();
    let output = replaced(response).expect("offloaded");

    let path = "/offload/web_search_call_7.txt";
    let backend = sessions.backend(&SessionId::from("s1"));
    assert_eq!(
        backend.read(path).await.unwrap(),
        payload,
        "full text stored"
    );
    let tokens = payload.len() / 4;
    let expected = format!(
        "{}\n\n[Tool result truncated: ~{tokens} tokens ({} bytes) offloaded to '{path}'. \
         Showing the first 10 lines. Use read_file(path=\"{path}\", offset=..., limit=...) \
         to read the rest, or ls(\"/offload\") to list offloaded results.]",
        lines(10),
        payload.len()
    );
    assert_eq!(output.value, JsonValue::String(expected));
    assert_eq!(output.metadata["offloaded_to"], path);
    assert_eq!(output.metadata["original_tokens"], tokens as u64);
    assert_eq!(output.metadata["original_bytes"], payload.len() as u64);
    assert_eq!(output.metadata["source"], "search", "tool metadata kept");
}

#[tokio::test]
async fn results_that_are_not_offloaded_continue() {
    let sessions = Arc::new(MemorySessions::new());
    let hook = offload(sessions.clone(), 100);
    let big = || Ok(ToolOutput::new(lines(200)));
    let at_threshold = Ok(ToolOutput::new("x".repeat(403)));
    let cases: Vec<(&str, ToolResult)> = vec![
        ("small", at_threshold),
        ("read_file", big()),
        ("ls", big()),
        ("failed", Err(ToolOutputError::new(lines(200)))),
        (
            "json",
            Ok(ToolOutput::new(serde_json::json!({"rows": lines(200)}))),
        ),
    ];
    for (tool, result) in cases {
        let response = hook.call(&request(tool, result)).await.unwrap();
        assert_eq!(response, PostToolUseResponse::Continue, "{tool}");
    }
    let backend = sessions.backend(&SessionId::from("s1"));
    assert!(
        backend.ls("").await.unwrap().is_empty(),
        "nothing was written"
    );
}

#[test]
fn a_bad_path_prefix_is_refused_up_front() {
    let mut config = OffloadConfig::new(1);
    config.path_prefix = "/a/../b".into();
    let files: Arc<dyn SessionFiles> = Arc::new(MemorySessions::new());
    assert!(ResultOffload::new(files, config).is_err());
}

/// A backend whose writes fail.
struct Full;

#[async_trait]
impl FileBackend for Full {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        Err(VfsError::NotFound { path: path.into() })
    }
    async fn write(&self, _: &str, _: &str) -> Result<(), VfsError> {
        Err(VfsError::Backend("disk full".into()))
    }
    async fn edit(&self, path: &str, _: &str, _: &str, _: bool) -> Result<usize, VfsError> {
        Err(VfsError::NotFound { path: path.into() })
    }
    async fn ls(&self, _: &str) -> Result<Vec<String>, VfsError> {
        Ok(Vec::new())
    }
    async fn exists(&self, _: &str) -> Result<bool, VfsError> {
        Ok(false)
    }
    async fn delete(&self, _: &str) -> Result<(), VfsError> {
        Ok(())
    }
}

#[tokio::test]
async fn a_failed_write_keeps_the_original_result_and_is_recorded() {
    let files = Arc::new(SharedFiles::new(Arc::new(Full)));
    let chain = HookChain::<PostToolUse>::default();
    let _registered = chain.register(Arc::new(offload(files, 10))).unwrap();
    let original: ToolResult = Ok(ToolOutput::new(lines(200)));
    let ran = chain.run(request("web_search", original.clone())).await;
    assert_eq!(ran.outcome, original, "the model still gets the result");
    match &ran.records[..] {
        [record] => match &record.status {
            HookStatus::Failed { error, fail_mode } => {
                assert!(error.contains("disk full"), "{error}");
                assert_eq!(*fail_mode, FailMode::Open);
            }
            other => panic!("expected a failed record, got {other:?}"),
        },
        other => panic!("expected one record, got {other:?}"),
    }
}
