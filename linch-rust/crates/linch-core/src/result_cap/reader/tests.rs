use std::sync::Arc;

use tokio_util::sync::CancellationToken;

use super::*;
use crate::ids::RunId;
use crate::message::{Message, Role, ToolResultBlock};
use crate::session_log::MessageEntry;
use crate::store::memory::MemorySessionStore;

const NS: &str = "ns";

fn entry(message: Message, visible: bool, historical: bool) -> LogEntry {
    LogEntry::Message(MessageEntry {
        message: Arc::new(message),
        visible,
        historical,
    })
}

fn result(id: &str, text: &str) -> Message {
    let block = ToolResultBlock::text(id, text, false);
    Message::new(Role::User, vec![ContentBlock::ToolResult(block)])
}

/// A session whose `t1` result is cut in the view and whole in the audit.
async fn tool(text: &str) -> (ReadToolResultTool, SessionId) {
    let store = Arc::new(MemorySessionStore::new());
    let session = SessionId::from("s1");
    store.create(NS, &session).await.unwrap();
    let entries = vec![
        entry(result("t1", text), false, true),
        entry(result("t1", "[cut]"), true, false),
        entry(result("t2", "short"), true, true),
    ];
    store.append(NS, &session, 0, entries).await.unwrap();
    let tool = ReadToolResultTool::new(store, NS, &ToolResultCap::new(128));
    (tool, session)
}

fn ctx(session: &SessionId) -> ToolContext {
    let (run, cancel) = (RunId::from("r1"), CancellationToken::new());
    ToolContext::new(session.clone(), run, "toolu_read", None, cancel, None)
}

fn input(value: serde_json::Value) -> JsonObject {
    value.as_object().unwrap().clone()
}

async fn read(tool: &ReadToolResultTool, session: &SessionId, input: JsonObject) -> ToolResult {
    tool.execute(input, ctx(session)).await
}

fn text(output: &ToolOutput) -> &str {
    output.value.as_str().unwrap()
}

#[tokio::test]
async fn it_returns_the_audit_text_not_the_cut_one() {
    let body = "0123456789".repeat(10);
    let (tool, session) = tool(&body).await;
    let out = read(
        &tool,
        &session,
        input(serde_json::json!({"tool_use_id": "t1"})),
    )
    .await
    .unwrap();
    assert!(text(&out).starts_with(&body), "{}", text(&out));
    assert!(text(&out).contains("end of the result"));
    assert_eq!(out.metadata["total_bytes"], 100);
}

#[tokio::test]
async fn paging_by_the_next_offset_reaches_the_end_without_a_gap() {
    let body: String = (0..1000)
        .map(|i| char::from(b'a' + (i % 26) as u8))
        .collect();
    let (tool, session) = tool(&body).await;
    // The budget of a 128-token cap is 512 bytes: a slice is at most 256.
    let (mut offset, mut got, mut pages) = (0, String::new(), 0);
    loop {
        let args = input(serde_json::json!({"tool_use_id": "t1", "offset": offset}));
        let out = read(&tool, &session, args).await.unwrap();
        let (slice, _) = text(&out).rsplit_once("\n[... ").unwrap();
        assert!(slice.len() <= 256, "{}", slice.len());
        got.push_str(slice);
        pages += 1;
        let next = out.metadata["next_offset"].as_u64().unwrap() as usize;
        if next >= body.len() {
            break;
        }
        offset = next;
    }
    assert_eq!(got, body);
    assert_eq!(pages, 4);
}

#[tokio::test]
async fn a_limit_is_clamped_and_a_small_one_reads_less() {
    let body = "x".repeat(1000);
    let (tool, session) = tool(&body).await;
    let big = input(serde_json::json!({"tool_use_id": "t1", "limit": 100_000}));
    let out = read(&tool, &session, big).await.unwrap();
    assert_eq!(out.metadata["next_offset"], 256);
    let small = input(serde_json::json!({"tool_use_id": "t1", "offset": 10, "limit": 5}));
    let out = read(&tool, &session, small).await.unwrap();
    assert!(text(&out).starts_with("xxxxx\n[... bytes 10..15 of 1000; read on with"));
}

#[tokio::test]
async fn an_unknown_id_or_a_bad_offset_is_a_clear_error() {
    let (tool, session) = tool("abc").await;
    let unknown = input(serde_json::json!({"tool_use_id": "nope"}));
    let error = read(&tool, &session, unknown).await.unwrap_err();
    assert_eq!(
        error.message,
        "no tool result with tool_use_id \"nope\" in this session"
    );
    let past = input(serde_json::json!({"tool_use_id": "t1", "offset": 3}));
    let error = read(&tool, &session, past).await.unwrap_err();
    assert_eq!(
        error.message,
        "offset 3 is past the end of the result (3 bytes)"
    );
    // Another session's results are not reachable.
    let other = SessionId::from("s2");
    let error = read(
        &tool,
        &other,
        input(serde_json::json!({"tool_use_id": "t1"})),
    )
    .await
    .unwrap_err();
    assert!(
        error.message.contains("no tool result") || error.message.contains("could not be read")
    );
}

#[test]
fn a_window_stays_on_character_boundaries() {
    let text = "a漢b";
    // `漢` takes bytes 1..4.
    assert_eq!(window(text, 2, 1), (1, 4));
    assert_eq!(window(text, 0, 2), (0, 1));
    assert_eq!(window(text, 4, 10), (4, 5));
    assert_eq!(window(text, 99, 10), (5, 5));
}

#[test]
fn the_tool_is_a_read_scope_tool_that_tool_search_keeps() {
    let store = Arc::new(MemorySessionStore::new());
    let tool = ReadToolResultTool::new(store, NS, &ToolResultCap::new(8000));
    assert_eq!(tool.scope(), ToolScope::Read);
    assert!(tool.parallel(&JsonObject::new()));
    assert!(tool.always_load());
    assert_eq!(tool.name(), READ_TOOL);
    // 32000 bytes of budget less a 512 byte footer allowance.
    assert_eq!(tool.max_limit, 31_488);
}
