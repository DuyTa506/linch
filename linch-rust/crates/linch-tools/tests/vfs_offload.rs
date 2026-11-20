//! `ResultOffload`: oversized tool results go to the session's backend and
//! the model sees a preview plus a pointer.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use linch_core::hooks::{HookChain, PostToolUse, PostToolUseRequest, PostToolUseResponse};
use linch_core::{
    FailMode, Hook, HookStatus, JsonObject, JsonValue, RunId, SessionId, ToolOutput,
    ToolOutputError, ToolResult,
};
use linch_tools::vfs::{
    DiskBackend, FileBackend, MemoryBackend, MemorySessions, OffloadConfig, ResultOffload,
    SessionFiles, SharedFiles, VfsError, offload_path,
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

/// The path `output` says it was offloaded to, which must start with `head`
/// and end in `.txt`.
fn named(output: &ToolOutput, head: &str) -> String {
    let path = output.metadata["offloaded_to"].as_str().unwrap().to_owned();
    assert!(path.starts_with(head) && path.ends_with(".txt"), "{path}");
    path
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

    let path = offload_path("/offload", "s1", "web_search", "call_7", &payload);
    let path = path.as_str();
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
         to read the rest, or ls(\"/offload/s1\") to list offloaded results.]",
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

    let path = named(&output, "/offload/s1/query_call_7_");
    let path = path.as_str();
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

    let path = offload_path("/offload", "s1", "Bash", "call_7", &message);
    let path = path.as_str();
    let backend = sessions.backend(&SessionId::from("s1"));
    assert_eq!(backend.read(path).await.unwrap(), message);
    let tokens = message.len() / 4;
    let expected = format!(
        "{}\n\n[Tool result truncated: ~{tokens} tokens ({} bytes) offloaded to '{path}'. \
         Showing the first 10 lines. Use read_file(path=\"{path}\", offset=..., limit=...) \
         to read the rest, or ls(\"/offload/s1\") to list offloaded results.]",
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
    let path = offload_path("/offload", "s1", "Bash", "call_7", &lines(200));
    assert_eq!(error.details.unwrap()["offloaded_to"], path.as_str());
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

fn request_of(session: &str, call: &str, text: &str) -> PostToolUseRequest {
    PostToolUseRequest {
        session_id: SessionId::from(session),
        run_id: RunId::from("r1"),
        tool_use_id: call.into(),
        tool_name: "export".into(),
        input: JsonObject::new(),
        result: Ok(ToolOutput::new(text)),
    }
}

/// The path a replacement points at.
fn offloaded_to(response: PostToolUseResponse) -> String {
    let output = replaced(response).expect("offloaded");
    output.metadata["offloaded_to"].as_str().unwrap().to_owned()
}

#[tokio::test]
async fn an_offloaded_result_is_not_overwritten_by_another_session() {
    let backend = Arc::new(MemoryBackend::new());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let alice = "alice's rows\n".repeat(50);
    let bob = "bob's rows\n".repeat(50);
    // A provider that names the first call of every response `call_0`.
    let first = hook.call(&request_of("s-alice", "call_0", &alice)).await;
    let second = hook.call(&request_of("s-bob", "call_0", &bob)).await;
    let (first, second) = (offloaded_to(first.unwrap()), offloaded_to(second.unwrap()));
    assert_ne!(first, second);
    assert_eq!(backend.read(&first).await.unwrap(), alice);
    assert_eq!(backend.read(&second).await.unwrap(), bob);
}

#[tokio::test]
async fn a_repeated_call_id_in_one_session_does_not_overwrite_the_earlier_result() {
    let backend = Arc::new(MemoryBackend::new());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let turn_one = "turn one\n".repeat(50);
    let turn_two = "turn two\n".repeat(50);
    let first = hook.call(&request_of("s1", "call_0", &turn_one)).await;
    let second = hook.call(&request_of("s1", "call_0", &turn_two)).await;
    let (first, second) = (offloaded_to(first.unwrap()), offloaded_to(second.unwrap()));
    assert_ne!(first, second);
    assert_eq!(backend.read(&first).await.unwrap(), turn_one);
    assert_eq!(backend.read(&second).await.unwrap(), turn_two);
}

#[tokio::test]
async fn a_call_or_session_id_with_path_syntax_is_still_offloaded_under_the_prefix() {
    let backend = Arc::new(MemoryBackend::new());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let text = "rows\n".repeat(50);
    let mut paths = Vec::new();
    for (session, call) in [
        ("s1", "../../etc/x"),
        ("s1", "a/b"),
        ("s1", ".."),
        ("..", "call_0"),
        (".", "call_0"),
        ("a/b", "call_0"),
        ("a_b", "call_0"),
    ] {
        let response = hook.call(&request_of(session, call, &text)).await;
        let path = offloaded_to(response.unwrap());
        assert!(path.starts_with("/offload/"), "{path}");
        let parts: Vec<&str> = path.split('/').collect();
        assert_eq!(parts.len(), 4, "/offload/<session>/<file>: {path}");
        assert!(parts.iter().all(|part| *part != ".." && *part != "."));
        assert_eq!(backend.read(&path).await.unwrap(), text, "{path}");
        paths.push(path);
    }
    paths.sort();
    paths.dedup();
    assert_eq!(paths.len(), 7, "every id gets its own file: {paths:?}");
}

/// A session or call id that fits a file name on its own must still give a
/// file name the disk accepts, whatever bytes it holds.
#[tokio::test]
async fn a_long_or_non_ascii_id_is_still_offloaded_to_disk() {
    let dir = tempfile::tempdir().unwrap();
    let backend = Arc::new(DiskBackend::new(dir.path()).unwrap());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let text = "rows\n".repeat(50);
    let mail = format!("{}@example.com", "người.dùng+".repeat(6));
    let ids = [
        ("ề".repeat(30), "call_0".to_owned()),
        (mail, "call_0".to_owned()),
        ("s1".to_owned(), "é".repeat(50)),
        ("s".repeat(300), "call_0".to_owned()),
        ("s1".to_owned(), "c".repeat(100)),
    ];
    let mut paths = Vec::new();
    for (session, call) in &ids {
        let response = hook.call(&request_of(session, call, &text)).await;
        let path = offloaded_to(response.expect("the write succeeds"));
        assert_eq!(backend.read(&path).await.unwrap(), text, "{path}");
        paths.push(path);
    }
    paths.sort();
    paths.dedup();
    assert_eq!(paths.len(), ids.len(), "distinct ids stay distinct");
}

/// No path segment is long, and ids that differ only past what a segment
/// keeps of them still get their own file.
#[tokio::test]
async fn ids_that_share_a_long_prefix_get_their_own_files() {
    let backend = Arc::new(MemoryBackend::new());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let text = "rows\n".repeat(50);
    let mut paths = Vec::new();
    for tail in ["a", "b"] {
        for session in [format!("{}{tail}", "s".repeat(200)), format!("é{tail}")] {
            let response = hook.call(&request_of(&session, "call_0", &text)).await;
            paths.push(offloaded_to(response.unwrap()));
        }
    }
    assert!(
        paths
            .iter()
            .all(|path| path.split('/').all(|part| part.len() <= 120))
    );
    paths.sort();
    paths.dedup();
    assert_eq!(paths.len(), 4, "{paths:?}");
}

#[tokio::test]
async fn an_empty_session_id_still_gets_a_directory() {
    let backend = Arc::new(MemoryBackend::new());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let text = "rows\n".repeat(50);
    let response = hook.call(&request_of("", "call_0", &text)).await;
    let path = offloaded_to(response.unwrap());
    assert_eq!(
        path.split('/').count(),
        4,
        "/offload/<session>/<file>: {path}"
    );
    assert_eq!(backend.read(&path).await.unwrap(), text);
}

/// A backend that yields on every call, as a database round trip does, and
/// counts the calls other than writes.
#[derive(Default)]
struct Counting {
    inner: MemoryBackend,
    probes: AtomicUsize,
}

#[async_trait]
impl FileBackend for Counting {
    async fn read(&self, path: &str) -> Result<String, VfsError> {
        self.probes.fetch_add(1, Ordering::SeqCst);
        self.inner.read(path).await
    }
    async fn write(&self, path: &str, content: &str) -> Result<(), VfsError> {
        tokio::task::yield_now().await;
        self.inner.write(path, content).await
    }
    async fn edit(&self, path: &str, old: &str, new: &str, all: bool) -> Result<usize, VfsError> {
        self.inner.edit(path, old, new, all).await
    }
    async fn ls(&self, prefix: &str) -> Result<Vec<String>, VfsError> {
        self.probes.fetch_add(1, Ordering::SeqCst);
        self.inner.ls(prefix).await
    }
    async fn exists(&self, path: &str) -> Result<bool, VfsError> {
        self.probes.fetch_add(1, Ordering::SeqCst);
        let found = self.inner.exists(path).await;
        tokio::task::yield_now().await;
        found
    }
    async fn delete(&self, path: &str) -> Result<(), VfsError> {
        self.inner.delete(path).await
    }
}

/// Two writers of one provider call id, at once, on one shared backend:
/// each pointer keeps naming its own result.
#[tokio::test]
async fn two_concurrent_results_of_one_call_id_do_not_overwrite_each_other() {
    let backend = Arc::new(Counting::default());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let one = "agent one's rows\n".repeat(50);
    let two = "agent two's rows\n".repeat(50);
    let (first, second) = (
        request_of("s1", "call_0", &one),
        request_of("s1", "call_0", &two),
    );
    let (first, second) = tokio::join!(hook.call(&first), hook.call(&second));
    let (first, second) = (offloaded_to(first.unwrap()), offloaded_to(second.unwrap()));
    assert_ne!(first, second);
    assert_eq!(backend.read(&first).await.unwrap(), one);
    assert_eq!(backend.read(&second).await.unwrap(), two);
}

/// The same result of the same call id again (a retry, or a loop) names the
/// same file, and no backend call but the write is made to find a free name.
#[tokio::test]
async fn a_repeat_of_a_call_id_costs_no_extra_backend_call() {
    let backend = Arc::new(Counting::default());
    let hook = offload(Arc::new(SharedFiles::new(backend.clone())), 10);
    let text = "rows\n".repeat(50);
    let mut paths = Vec::new();
    for _ in 0..200 {
        let response = hook.call(&request_of("s1", "call_0", &text)).await;
        paths.push(offloaded_to(response.unwrap()));
    }
    assert_eq!(backend.probes.load(Ordering::SeqCst), 0);
    paths.dedup();
    assert_eq!(paths.len(), 1, "one result is one file: {paths:?}");
}
