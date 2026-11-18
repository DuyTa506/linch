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
        PostToolUseResponse::Mutate { .. } => panic!("the offload replaces, it never mutates"),
    }
}

/// The error of a replacement that is an error.
fn replaced_error(response: PostToolUseResponse) -> ToolOutputError {
    match response {
        PostToolUseResponse::Replace { result } => result.expect_err("an error result"),
        other => panic!("expected a replacement, got {other:?}"),
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

/// One very long line (a minified file) is not a ten-line preview: the
/// replacement must be far smaller than the result it replaces.
#[tokio::test]
async fn a_single_huge_line_is_not_previewed_whole() {
    let hook = offload(Arc::new(MemorySessions::new()), 100);
    let payload = "x".repeat(200_000);
    let response = hook
        .call(&request("web_search", Ok(ToolOutput::new(payload))))
        .await
        .unwrap();
    let JsonValue::String(shown) = replaced(response).expect("offloaded").value else {
        panic!("a string pointer");
    };
    assert!(
        shown.len() < 4_000,
        "the pointer holds {} bytes",
        shown.len()
    );
    assert!(
        shown.contains("Showing the first 1 lines (cut at 2000 bytes)."),
        "the cut is named: {shown}"
    );
}

/// The wording is the same when the preview is not cut by the byte bound.
#[tokio::test]
async fn an_uncut_preview_says_ten_lines_and_no_cut() {
    let hook = offload(Arc::new(MemorySessions::new()), 100);
    let response = hook
        .call(&request("web_search", Ok(ToolOutput::new(lines(200)))))
        .await
        .unwrap();
    let JsonValue::String(shown) = replaced(response).expect("offloaded").value else {
        panic!("a string pointer");
    };
    assert!(shown.contains("Showing the first 10 lines. Use read_file"));
    assert!(!shown.contains("cut at"));
}

#[test]
fn the_policy_version_marks_the_wider_coverage() {
    assert_eq!(
        offload(Arc::new(MemorySessions::new()), 1).policy_version(),
        2
    );
}

/// 300 KB of rows, as a host or MCP tool returns JSON.
fn rows(n: usize) -> JsonValue {
    let rows: Vec<JsonValue> = (0..n)
        .map(|i| serde_json::json!({"id": i, "name": format!("row {i}"), "tags": ["a", "b"]}))
        .collect();
    serde_json::json!({"rows": rows, "total": n})
}

#[tokio::test]
async fn a_structured_value_is_stored_pretty_printed_and_replaced_by_a_pointer() {
    let sessions = Arc::new(MemorySessions::new());
    let hook = offload(sessions.clone(), 100);
    let value = rows(3_000);
    let compact = serde_json::to_string(&value).unwrap();
    let mut output = ToolOutput::new(value.clone());
    output.metadata.insert("source".into(), "mcp".into());
    let response = hook.call(&request("query", Ok(output))).await.unwrap();
    let output = replaced(response).expect("offloaded");

    let path = "/offload/query_call_7.txt";
    let stored = sessions
        .backend(&SessionId::from("s1"))
        .read(path)
        .await
        .unwrap();
    assert_eq!(stored, serde_json::to_string_pretty(&value).unwrap());
    assert!(stored.lines().count() > 3_000, "one value per line");
    assert_eq!(serde_json::from_str::<JsonValue>(&stored).unwrap(), value);
    let JsonValue::String(shown) = output.value else {
        panic!("the model sees a string pointer");
    };
    let tokens = compact.len() / 4;
    assert!(shown.starts_with("{\n  \"rows\": [\n"), "{shown}");
    assert!(shown.contains(&format!(
        "[Tool result truncated: ~{tokens} tokens ({} bytes) offloaded to '{path}'. \
         Showing the first 10 lines.",
        compact.len()
    )));
    assert!(!shown.contains("row 2999"), "the payload is not inline");
    assert_eq!(output.metadata["offloaded_to"], path);
    assert_eq!(output.metadata["original_tokens"], tokens as u64);
    assert_eq!(output.metadata["original_bytes"], compact.len() as u64);
    assert_eq!(output.metadata["original_kind"], "json");
    assert_eq!(output.metadata["source"], "mcp", "tool metadata kept");
    assert_eq!(output.metadata.len(), 5, "sizes only, never the payload");
}

#[tokio::test]
async fn a_structured_value_under_the_threshold_passes_through() {
    let hook = offload(Arc::new(MemorySessions::new()), 100);
    for value in [rows(2), serde_json::json!(7), serde_json::json!(null)] {
        let response = hook
            .call(&request("query", Ok(ToolOutput::new(value))))
            .await
            .unwrap();
        assert_eq!(response, PostToolUseResponse::Continue);
    }
}

#[tokio::test]
async fn a_long_error_message_is_stored_and_keeps_its_code() {
    let sessions = Arc::new(MemorySessions::new());
    let hook = offload(sessions.clone(), 100);
    let message = lines(200);
    let mut error = ToolOutputError::new(message.clone());
    error.code = Some("exit_status".into());
    error.details = Some(serde_json::json!({"exit_code": 2}));
    let response = hook.call(&request("Bash", Err(error))).await.unwrap();
    let error = replaced_error(response);

    let path = "/offload/Bash_call_7.txt";
    let backend = sessions.backend(&SessionId::from("s1"));
    assert_eq!(backend.read(path).await.unwrap(), message);
    let tokens = message.len() / 4;
    let expected = format!(
        "{}\n\n[Tool result truncated: ~{tokens} tokens ({} bytes) offloaded to '{path}'. \
         Showing the first 10 lines. Use read_file(path=\"{path}\", offset=..., limit=...) \
         to read the rest, or ls(\"/offload\") to list offloaded results.]",
        lines(10),
        message.len()
    );
    assert_eq!(error.message, expected);
    assert_eq!(error.code.as_deref(), Some("exit_status"), "the code stays");
    let details = error.details.expect("details stay");
    assert_eq!(details["exit_code"], 2);
    assert_eq!(details["offloaded_to"], path);
    assert_eq!(details["original_tokens"], tokens as u64);
    assert_eq!(details["original_bytes"], message.len() as u64);
}

#[tokio::test]
async fn an_error_without_details_gets_the_sizes_as_its_details() {
    let hook = offload(Arc::new(MemorySessions::new()), 100);
    let response = hook
        .call(&request("Bash", Err(ToolOutputError::new(lines(200)))))
        .await
        .unwrap();
    let error = replaced_error(response);
    assert_eq!(error.code, None);
    assert_eq!(
        error.details.unwrap()["offloaded_to"],
        "/offload/Bash_call_7.txt"
    );
}

#[tokio::test]
async fn a_short_error_passes_through() {
    let hook = offload(Arc::new(MemorySessions::new()), 100);
    let response = hook
        .call(&request("Bash", Err(ToolOutputError::new("exit 1"))))
        .await
        .unwrap();
    assert_eq!(response, PostToolUseResponse::Continue);
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
        ("failed", Err(ToolOutputError::new("exit 1"))),
        (
            "json",
            Ok(ToolOutput::new(serde_json::json!({"rows": [1]}))),
        ),
        (
            "read_file",
            Ok(ToolOutput::new(serde_json::json!(rows(200)))),
        ),
        ("ls", Err(ToolOutputError::new(lines(200)))),
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
